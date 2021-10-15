from abc import ABC, abstractproperty
from typing import Any, Dict, Generic, List, Optional, Tuple, TypeVar

import datetime
import hashlib
import logging

import docker
import docker.models.services as docker_services
import docker.models.secrets as docker_secrets
import docker.models.configs as docker_configs
import docker.types as docker_types

NAMESPACE = "ndi"
SECRET_NGINX_CONF = f"{NAMESPACE}.conf"
SECRET_ACME_ACCOUNT = f"{NAMESPACE}.acct"
SECRET_SVC_BASE = f"{NAMESPACE}.svc"
SECRET_DHPARAM_BASE = f"{NAMESPACE}.dhparam"
CONFIG_CHALLENGE_BASE = f"{NAMESPACE}.challange"

logger = logging.getLogger(__name__)


class DockerAdapter:
    client: docker.DockerClient

    def __init__(self, client: docker.DockerClient) -> None:
        self.client = client

    def list_secrets(self, prefix: Optional[str] = None):
        secrets = self.client.secrets.list()
        if prefix is not None:
            secrets = [secret for secret in secrets if secret.name.startswith(prefix)]
        return secrets

    def secret_exists(self, secret_name: str) -> Optional[str]:
        try:
            return self.client.secrets.get(secret_name).id
        except docker.errors.NotFound:
            return None

    def secret_reference(
        self, secret_id: str, secret_name: str, target: str
    ) -> docker_types.SecretReference:
        return docker_types.SecretReference(secret_id, secret_name, target, mode=440)

    def read_secret(self, secret_name: str) -> Optional[docker_secrets.Model]:
        try:
            return self.client.secrets.get(secret_name)
        except docker.errors.NotFound:
            return None

    def del_secret(self, secret_name: str) -> bool:
        try:
            self.client.secrets.get(secret_name).remove()
            return True
        except docker.errors.NotFound:
            return False

    def write_secret(
        self, secret_name: str, secret: str, labels: Optional[Dict[str, str]] = None
    ) -> docker_secrets.Model:
        if labels is None:
            labels = {}
        self.del_secret(secret_name)
        return self.client.secrets.create(name=secret_name, data=secret, labels=labels)

    def config_read(self, config_name: str) -> Optional[docker_configs.Model]:
        try:
            return self.client.configs.get(config_name)
        except docker.errors.NotFound:
            return None

    def config_del(self, config_name: str) -> bool:
        try:
            self.client.configs.get(config_name).remove()
            return True
        except docker.errors.NotFound:
            return False

    def config_write(
        self, config_name: str, config: str, labels: Optional[Dict[str, str]] = None
    ) -> docker_configs.Model:
        if labels is None:
            labels = {}
        self.config_del(config_name)
        return self.client.configs.create(name=config_name, data=config, labels=labels)

    @property
    def services(self) -> List["ServiceAdapter"]:
        return [
            ServiceAdapter(self, service)
            for service in self.client.services.list(
                filters=dict(label="nginx-ingress.host")
            )
        ]


class ServiceAdapter:
    LABELS = ("hosts", "port", "path", "acme_ssl", "ssl_redirect")

    docker: DockerAdapter
    service: docker_services.Model

    def __init__(self, docker: DockerAdapter, service: docker_services.Model) -> None:
        self.docker = docker
        self.service = service

    def __repr__(self) -> str:
        labels = []
        for label in self.LABELS:
            value = getattr(self, label)
            if value is True:
                labels.append(label)
            elif value:
                labels.append(f"{label} {value}")

        return f"<ServiceAdapter: {repr(self.service)} {', '.join(labels)}>"

    @property
    def labels(self) -> Dict[str, str]:
        if "Labels" in self.service.attrs["Spec"]:
            return self.service.attrs["Spec"]["Labels"]
        return {}

    @property
    def hosts(self) -> List[str]:
        if "nginx-ingress.host" not in self.labels:
            return []
        return self.labels["nginx-ingress.host"].split(",")

    @property
    def port(self) -> int:
        if "nginx-ingress.port" not in self.labels:
            return 80
        return int(self.labels["nginx-ingress.port"])

    @property
    def path(self) -> str:
        if "nginx-ingress.path" not in self.labels:
            return "/"
        return self.labels["nginx-ingress.path"]

    @property
    def acme_ssl(self) -> bool:
        return "nginx-ingress.ssl" in self.labels

    @property
    def ssl_redirect(self) -> bool:
        return "nginx-ingress.ssl-redirect" in self.labels

    @property
    def keys(self) -> "VersionedSecrets":
        return VersionedSecrets(
            self.docker, f"{SECRET_SVC_BASE}.{self.service.id}.key."
        )

    @property
    def certs(self) -> "VersionedSecrets":
        return VersionedSecrets(
            self.docker, f"{SECRET_SVC_BASE}.{self.service.id}.crt."
        )

    @property
    def latest_cert_pair_with_version(
        self
    ) -> Optional[Tuple[docker_secrets.Model, docker_secrets.Model, int]]:
        common = self.keys.common_versions(self.certs)
        if not common:
            return None
        max_key = max(common.keys())

        return common[max_key] + (max_key,)

    @property
    def latest_cert_pair(
        self
    ) -> Optional[Tuple[docker_secrets.Model, docker_secrets.Model]]:
        latest = self.latest_cert_pair_with_version
        if latest is None:
            return None
        return latest[:2]

    @property
    def latest_cert_version(self) -> Optional[int]:
        latest = self.latest_cert_pair_with_version
        if latest is None:
            return None
        return latest[2]

    @property
    def cert_renewable(self) -> bool:
        latest_cert_pair = self.latest_cert_pair
        if not latest_cert_pair:
            return False
        cert = latest_cert_pair[1]

        secret_expiry_unix = float(cert.attrs["Spec"]["Labels"]["expires"])
        secret_expiry = datetime.datetime.utcfromtimestamp(secret_expiry_unix)

        if secret_expiry < datetime.datetime.now() + datetime.timedelta(days=7):
            return True

        return False


class SecretContainer:
    def __init__(self, config: str, metadata: Optional[Dict[str, Any]]) -> None:
        self.config = config
        self.metadata = metadata or {}

    def __str__(self) -> str:
        return self.config

    def __repr__(self) -> str:
        return f"<SecretContainer: {self.hash}>"

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.config.encode("utf-8")).hexdigest()


T = TypeVar("T")


class VersionedBase(ABC, Generic[T]):
    def __init__(self, docker: DockerAdapter, prefix: str) -> None:
        self.docker = docker
        self.prefix = prefix

    @abstractproperty
    def load_list(self) -> List[T]:
        ...

    @property
    def versions(self) -> Dict[int, T]:
        return {int(config.name.split(".")[-1]): config for config in self.load_list}

    @property
    def latest_version(self) -> Optional[Tuple[int, T]]:
        versions = self.versions
        if not versions:
            return None

        max_version = max(versions.keys())

        return (max_version, versions[max_version])

    def common_versions(self, other: "VersionedBase[T]") -> Dict[int, Tuple[T, T]]:
        my_versions = self.versions
        other_versions = other.versions

        return {
            version: (my_versions[version], other_versions[version])
            for version in set(my_versions.keys()).intersection(other_versions.keys())
        }

    def with_version(self, version: int) -> str:
        prefix = self.prefix
        if not prefix.endswith("."):
            prefix += "."
        return f"{self.prefix}{version}"


class VersionedSecrets(VersionedBase[docker_secrets.Model]):
    @property
    def load_list(self) -> List[docker_secrets.Model]:
        return self.docker.list_secrets(self.prefix)
