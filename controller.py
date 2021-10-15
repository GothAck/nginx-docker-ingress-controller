import datetime
import subprocess
from typing import Dict, List, Optional, Tuple

import logging
from time import sleep, mktime
import sys

import docker
import docker.models.services as docker_services
from docker.types.services import EndpointSpec, RestartPolicy
from jinja2 import Template

from common import (
    SECRET_DHPARAM_BASE,
    DockerAdapter,
    SecretContainer,
    SECRET_NGINX_CONF,
    SECRET_ACME_ACCOUNT,
    VersionedSecrets,
)

logging.basicConfig()
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("controller")
logger.setLevel(logging.DEBUG)

adapter = DockerAdapter(docker.from_env())


class Controller:
    adapter: DockerAdapter
    config_template: Template

    def __init__(self) -> None:
        self.adapter = DockerAdapter(docker.from_env())
        with open("nginx.conf.jinja") as template:
            self.config_template = Template(template.read())

    @property
    def nginx_service(self) -> Optional[docker_services.Model]:
        try:
            return self.adapter.client.services.get("nginx-docker-ingress-nginx")
        except docker.errors.NotFound:
            return None

    @property
    def nginx_service_secrets(self) -> Dict[str, str]:
        secrets = {}
        service = self.nginx_service
        if not service:
            return secrets
        for secret in service.attrs["Spec"]["TaskTemplate"]["ContainerSpec"]["Secrets"]:
            secrets[secret["File"]["Name"]] = secret["SecretName"]
        return secrets

    @property
    def nginx_service_config(self) -> Optional[str]:
        return self.nginx_service_secrets.get("/etc/nginx/nginx.conf")

    @property
    def nginx_config(self) -> SecretContainer:
        services = self.adapter.services
        logger.debug("Generating Nginx config, services %r", services)
        return SecretContainer(
            self.config_template.render(services=services),
            metadata=dict(cert_pairs=map(lambda s: s.latest_cert_pair, services)),
        )

    def ensure_nginx_config(self) -> Tuple[str, str, SecretContainer]:
        config = self.nginx_config
        config_hash = config.hash
        config_secret_name = f"{SECRET_NGINX_CONF}.{config_hash}"
        secret_id = self.adapter.secret_exists(config_secret_name)
        if not secret_id:
            secret_id = self.adapter.write_secret(config_secret_name, str(config)).id
        return (config_hash, secret_id, config)

    def ensure_nginx_service(self) -> None:
        logger.info("Ensure nginx service")
        config_hash, config_secret_id, config = self.ensure_nginx_config()
        config_secret_name = f"{SECRET_NGINX_CONF}.{config_hash}"

        config_secret_ref = self.adapter.secret_reference(
            config_secret_id, config_secret_name, "/etc/nginx/nginx.conf"
        )

        _, dhparams_secret = self.dhparams_vs.latest_version or (None, None)
        assert dhparams_secret, "dhparams secret missing"
        dhparams_secret_ref = self.adapter.secret_reference(
            dhparams_secret.id, dhparams_secret.name, "/etc/nginx/ssl-dhparams.pem"
        )

        cert_pair_secret_refs = []
        for cert_pair in config.metadata["cert_pairs"]:
            if not cert_pair:
                continue
            for model in cert_pair:
                cert_pair_secret_refs.append(
                    self.adapter.secret_reference(model.id, model.name, model.name)
                )

        endpoint_spec = EndpointSpec(ports={9080: 80, 9443: 443})

        service = self.nginx_service
        if not service:
            logger.info("No nginx service found")
            service = self.adapter.client.services.create(
                image="gothack/docker-swarm-ingress:nginx-latest",
                name="nginx-docker-ingress-nginx",
                endpoint_spec=endpoint_spec,
                networks=["nginx-docker-ingress"],
                secrets=[config_secret_ref, dhparams_secret_ref]
                + cert_pair_secret_refs,
            )
            sleep(10)  # FIXME: sleep, really?!
            service.update()
            service.scale(2)
        else:
            current_config = self.nginx_service_config
            logger.info("Nginx service found with config %s", current_config)
            if current_config != config_secret_name:
                logger.info(
                    "Nginx config hash mismatch (%s vs %s), updating",
                    config_secret_name,
                    current_config,
                )
                service.update(
                    image="gothack/docker-swarm-ingress:nginx-latest",
                    endpoint_spec=endpoint_spec,
                    networks=["nginx-docker-ingress"],
                    secrets=[config_secret_ref, dhparams_secret_ref]
                    + cert_pair_secret_refs,
                )
                # TODO: Clean up old secret
                sleep(10)  # FIXME: sleep, really?!
                service.update()
                service.scale(2)
            else:
                logger.info("Nginx config match, skip update")

    @property
    def account_service(self) -> Optional[docker_services.Model]:
        try:
            return self.adapter.client.services.get("nginx-docker-ingress-account")
        except docker.errors.NotFound:
            return None

    def ensure_account(self) -> None:
        logger.info("Ensure acme account exists")
        if self.adapter.secret_exists(SECRET_ACME_ACCOUNT):
            # TODO: Validate account?
            return

        service = self.account_service

        if service:
            service.remove()

        service = self.adapter.client.services.create(
            image="gothack/docker-swarm-ingress:robot-latest",
            name="nginx-docker-ingress-account",
            command=["python", "robot.py", "ensure-account"],
            restart_policy=RestartPolicy(),
            mounts=["/var/run/docker.sock:/var/run/docker.sock:rw"],
        )

        while True:
            sleep(10)
            tasks = service.tasks()
            assert len(tasks) == 1, "Only one task started"
            state = tasks[0]["Status"]["State"]
            desired_state = tasks[0]["DesiredState"]
            exit_code = tasks[0]["Status"].get("ContainerStatus", {}).get("ExitCode")
            logger.info("%s %s %d", state, desired_state, exit_code)
            if state == "complete":
                break

    @property
    def robot_service(self) -> Optional[docker_services.Model]:
        try:
            return self.adapter.client.services.get("nginx-docker-ingress-robot")
        except docker.errors.NotFound:
            return None

    def run_robot(self) -> None:
        logger.info("Running the robot")

        account_secret_id = self.adapter.secret_exists(SECRET_ACME_ACCOUNT)
        if not account_secret_id:
            logger.exception("Failed to get account secret")
            return

        account_secret_ref = self.adapter.secret_reference(
            account_secret_id, SECRET_ACME_ACCOUNT, SECRET_ACME_ACCOUNT
        )

        robot_service = self.robot_service

        if robot_service:
            robot_service.remove()

        robot_service = self.adapter.client.services.create(
            image="gothack/docker-swarm-ingress:robot-latest",
            name="nginx-docker-ingress-robot",
            command=["python", "robot.py", "observe-and-obey"],
            restart_policy=RestartPolicy(),
            mounts=["/var/run/docker.sock:/var/run/docker.sock:rw"],
            networks=["nginx-docker-ingress"],
            secrets=[account_secret_ref],
        )

    @property
    def dhparams_vs(self):
        return VersionedSecrets(self.adapter, f"{SECRET_DHPARAM_BASE}.")

    def ensure_dhparams(self):
        logger.info("Ensuring dhparams is fresh")
        vs = self.dhparams_vs

        print(vs, vs.load_list, vs.versions)
        version, model = vs.latest_version or (None, None)
        logger.info("%r %r", version, model)
        if version is not None and model is not None:
            logger.info("Have dhparams secret, checking...")
            next_version = version + 1
            secret_expiry_unix = float(model.attrs["Spec"]["Labels"]["expires"])
            secret_expiry = datetime.datetime.utcfromtimestamp(secret_expiry_unix)
            logger.info(
                "%r %r",
                secret_expiry,
                datetime.datetime.now() + datetime.timedelta(days=7),
            )
            if secret_expiry > datetime.datetime.now() + datetime.timedelta(days=7):
                logger.info("Dhparams is fresh enough")
                return
        else:
            next_version = 0

        secret_name = vs.with_version(next_version)
        secert_expiry = datetime.datetime.utcnow() + datetime.timedelta(days=28)
        secert_expiry_unix = mktime(secert_expiry.timetuple())

        logger.info("Generating new dhparams")
        subprocess.run(
            ["openssl", "dhparam", "-out", "/tmp/ssl-dhparams.pem", "4096"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).check_returncode()

        logger.info("Storing dhparams")
        with open("/tmp/ssl-dhparams.pem") as f:
            self.adapter.write_secret(
                secret_name, f.read(), dict(expires=str(secert_expiry_unix))
            )


def main(argv: List[str]) -> int:
    logger.info("Booting Nginx docker ingress controller")
    controller = Controller()

    controller.ensure_account()

    controller.ensure_dhparams()

    controller.run_robot()

    while True:
        controller.ensure_nginx_service()
        sleep(10)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))