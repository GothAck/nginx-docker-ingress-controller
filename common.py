from abc import ABC, abstractproperty, abstractstaticmethod
from functools import lru_cache
from time import sleep
from typing import Any, Dict, Generic, List, Optional, Tuple, TypeVar, cast

import base64
import datetime
import hashlib
import logging

import docker
import docker.models.services as docker_services
import docker.models.secrets as docker_secrets
import docker.models.configs as docker_configs
import docker.types as docker_types
from docker.types.services import SecretReference, ServiceMode

from config import (
    ConfigRoot,
    ConfigServiceAccount,
    ConfigServiceBase,
    ConfigServiceChallenge,
    ConfigServiceNginx,
    ConfigServiceRobot,
    config_load_and_convert,
)

NAMESPACE = "ndi"
SECRET_NGINX_CONF = f"{NAMESPACE}.conf"
SECRET_ACME_ACCOUNT = f"{NAMESPACE}.acct"
SECRET_SVC_BASE = f"{NAMESPACE}.svc"
SECRET_DHPARAM_BASE = f"{NAMESPACE}.dhparam"
CONFIG_CHALLENGE_BASE = f"{NAMESPACE}.challange"
CONFIG_CONFIG_BASE = f"{NAMESPACE}.config"

logger = logging.getLogger(__name__)


class DockerAdapter:
    client: docker.DockerClient

    svc_account: "IngressService[ConfigServiceAccount]"
    svc_challenge: "IngressService[ConfigServiceChallenge]"
    svc_nginx: "IngressService[ConfigServiceNginx]"
    svc_robot: "IngressService[ConfigServiceRobot]"

    def __init__(self, client: docker.DockerClient) -> None:
        self.client = client

        self.svc_account = IngressService(self, self.config.services.account)
        self.svc_challenge = IngressService(self, self.config.services.challenge)
        self.svc_nginx = IngressService(self, self.config.services.nginx)
        self.svc_robot = IngressService(self, self.config.services.robot)

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

    def list_configs(self, prefix: Optional[str] = None):
        configs = self.client.configs.list()
        if prefix is not None:
            configs = [config for config in configs if config.name.startswith(prefix)]
        return configs

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

    @property
    @lru_cache()
    def config(self) -> Optional[ConfigRoot]:
        latest = VersionedConfigs(self, CONFIG_CONFIG_BASE).latest
        if not latest:
            raise Exception(
                f"Config missing, try adding a docker config called {CONFIG_CONFIG_BASE}.0"
            )

        data = base64.b64decode(latest.attrs["Spec"]["Data"]).decode("utf-8")

        return config_load_and_convert(data)


TConfigService = TypeVar("TConfigService", bound=ConfigServiceBase)


class ServiceAdapterBase(ABC):
    docker: DockerAdapter

    def __init__(self, docker: DockerAdapter) -> None:
        super().__init__()
        self.docker = docker

    @abstractproperty
    def model(self) -> Optional[docker_services.Model]:
        ...


class ServiceAdapter(ServiceAdapterBase):
    LABELS = ("hosts", "port", "path", "acme_ssl", "ssl_redirect")

    __model: docker_services.Model

    def __init__(self, docker: DockerAdapter, model: docker_services.Model) -> None:
        super().__init__(docker)
        self.__model = model

    def __repr__(self) -> str:
        labels = []
        for label in self.LABELS:
            value = getattr(self, label)
            if value is True:
                labels.append(label)
            elif value:
                labels.append(f"{label} {value}")

        return f"<ServiceAdapter: {repr(self.model)} {', '.join(labels)}>"

    @property
    def model(self) -> docker_services.Model:
        return self.__model

    @property
    def labels(self) -> Dict[str, str]:
        model = self.model
        if not model:
            return {}

        if "Labels" in model.attrs["Spec"]:
            return model.attrs["Spec"]["Labels"]
        return {}

    @property
    def hosts(self) -> List[str]:
        return list(filter(bool, self.labels.get("nginx-ingress.host", "").split(",")))

    @property
    def port(self) -> int:
        return int(self.labels.get("nginx-ingress.port", 80))

    @property
    def path(self) -> str:
        return self.labels.get("nginx-ingress.path", "")

    @property
    def acme_ssl(self) -> bool:
        return "nginx-ingress.ssl" in self.labels

    @property
    def ssl_redirect(self) -> bool:
        return "nginx-ingress.ssl-redirect" in self.labels

    @property
    def proxy_protocol(self) -> Optional[str]:
        return self.labels.get("nginx-ingress.proxy-protocol")

    @property
    def keys(self) -> "VersionedSecrets":
        model = self.model
        if not model:
            raise ReferenceError(f"Service {self.model.name} does not exist")
        return VersionedSecrets(self.docker, f"{SECRET_SVC_BASE}.{model.id}.key.")

    @property
    def certs(self) -> "VersionedSecrets":
        model = self.model
        if not model:
            raise ReferenceError(f"Service {self.model.name} does not exist")
        return VersionedSecrets(self.docker, f"{SECRET_SVC_BASE}.{model.id}.crt.")

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

    @property
    def secrets(self) -> Dict[str, str]:
        secrets = {}
        model = self.model
        if not model:
            return secrets

        for secret in model.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Secrets"]:
            secrets[secret["File"]["Name"]] = secret["SecretName"]

        return secrets


class IngressService(ServiceAdapterBase, Generic[TConfigService]):
    config: TConfigService

    def __init__(self, docker: DockerAdapter, config: TConfigService) -> None:
        super().__init__(docker)
        self.config = config

    def __repr__(self) -> str:
        return f"<IngressService: {repr(self.model)}>"

    @property
    def model(self) -> Optional[docker_services.Model]:
        try:
            return self.docker.client.services.get(self.config.name)
        except docker.errors.NotFound:
            return None

    def ensure(
        self,
        command: Optional[str] = None,
        networks: Optional[List[str]] = None,
        secrets: Optional[List[SecretReference]] = None,
        mounts: Optional[List[str]] = None,
    ) -> docker_services.Model:
        model = self.model
        config = self.config

        kwargs = {}
        if isinstance(config, ConfigServiceNginx):
            if config.attach_to_host_network:
                networks.append("host")
            kwargs = dict(
                preferences=map(lambda p: p.tuple, config.preferences),
                maxreplicas=config.maxreplicas,
                mode=ServiceMode(
                    mode=config.service_mode.value, replicas=config.replicas
                ),
                endpoint_spec=config.endpoint_spec,
            )

        if not model:
            logger.info("Service %s does not exist, creating", config.name)
            model = self.docker.client.services.create(
                image=config.image,
                name=config.name,
                command=command,
                networks=networks,
                secrets=secrets,
                mounts=mounts,
                constraints=config.constraints,
                labels=config.labels,
                **kwargs,
            )
        else:
            logger.info("Service %s exists, updating", config.name)
            model.update(
                image=config.image,
                name=config.name,
                command=command,
                networks=networks,
                secrets=secrets,
                mounts=mounts,
                constraints=config.constraints,
                labels=config.labels,
                **kwargs,
            )

        return model

    def wait_for_state(self, state_desired: str, states_invalid: List[str]) -> bool:
        logger.info("Waiting for %s state %s", self.config.name, state_desired)
        while True:
            sleep(5)
            tasks = self.model.tasks()
            states = set()

            for task in tasks:
                if task["DesiredState"] == "shutdown":
                    continue
                state = task["Status"]["State"]
                states.add(state)

            logger.debug("Current states %r", states)

            for state_invalid in states_invalid:
                if state_invalid in states:
                    logger.info(
                        "Invalid state detected %s in %r", state_invalid, states
                    )
                    return False

            if states == set([state_desired]):
                logger.info("States converged to %s", state_desired)
                return True


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
        if not prefix.endswith("."):
            prefix += "."

        self.docker = docker
        self.prefix = prefix

    @abstractproperty
    def list(self) -> List[T]:
        ...

    @abstractstaticmethod
    def get_name(config: T) -> str:
        ...

    @property
    def versions(self) -> Dict[int, T]:
        return {
            int(self.get_name(config).split(".")[-1]): config for config in self.list
        }

    @property
    def latest_version(self) -> Optional[Tuple[int, T]]:
        versions = self.versions
        if not versions:
            return None

        max_version = max(versions.keys())

        return (max_version, versions[max_version])

    @property
    def latest(self) -> Optional[T]:
        latest_version = self.latest_version
        if latest_version is None:
            return latest_version
        return latest_version[1]

    def common_versions(self, other: "VersionedBase[T]") -> Dict[int, Tuple[T, T]]:
        my_versions = self.versions
        other_versions = other.versions

        return {
            version: (my_versions[version], other_versions[version])
            for version in set(my_versions.keys()).intersection(other_versions.keys())
        }

    def with_version(self, version: int) -> str:
        return f"{self.prefix}{version}"


class VersionedSecrets(VersionedBase[docker_secrets.Model]):
    @property
    def list(self) -> List[docker_secrets.Model]:
        return self.docker.list_secrets(self.prefix)

    @staticmethod
    def get_name(config: docker_secrets.Model) -> str:
        return cast(str, config.name)


class VersionedConfigs(VersionedBase[docker_configs.Model]):
    @property
    def list(self) -> List[docker_configs.Model]:
        return self.docker.list_configs(self.prefix)

    @staticmethod
    def get_name(config: docker_configs.Model) -> str:
        return cast(str, config.name)
