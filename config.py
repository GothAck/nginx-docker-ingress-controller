from typing import Dict, List, Optional, Tuple

from enum import Enum
import re
import sys

from docker.types.services import EndpointSpec
from pydantic import BaseModel, validator
from pydantic.class_validators import root_validator
import yaml

RE_EMAIL = re.compile(r"^.+@.+$")


class PortPublishMode(str, Enum):
    ingress = "ingress"
    host = "host"
    none = "none"


class ServiceMode(str, Enum):
    replicated = "replicated"
    global_ = "global"


class ConfigAcme(BaseModel):
    email: str
    accept_tos: bool

    @validator("email")
    def email_valid(cls, v: str):
        if not RE_EMAIL.match(v):
            raise ValueError("Invalid email address")
        return v

    @validator("accept_tos")
    def accept_tos_valid(cls, v: bool):
        if not v:
            raise ValueError("You must accept the Let's Encrypt Terms Of Service")
        return v


class ConfigPorts(BaseModel):
    http: int = 80
    https: int = 443

    @validator("http", "https")
    def ports_valid(cls, v: int):
        if v < 1 or v > 65535:
            raise ValueError("Invalid port")
        return v


class ConfigPlacementPreference(BaseModel):
    strategy: str = "spread"
    descriptor: str

    @validator("strategy")
    def strategy_valid(cls, v: str):
        if v not in ["spread"]:
            raise ValueError("Invalid")
        return v

    def tuple(self) -> Tuple[str, str]:
        return (self.strategy, self.descriptor)


class ConfigServiceBase(BaseModel):
    name: str
    image: str
    constraints: List[str] = []
    labels: Dict[str, str] = {}


class ConfigServiceAccount(ConfigServiceBase):
    name: str = "nginx-docker-ingress-account"
    image: str = "gothack/docker-swarm-ingress:robot-latest"


class ConfigServiceChallenge(ConfigServiceBase):
    name: str = "nginx-docker-ingress-challenge"
    image: str = "gothack/docker-swarm-ingress:challenge-latest"


class ConfigServiceNginx(ConfigServiceBase):
    name: str = "nginx-docker-ingress-nginx"
    image: str = "gothack/docker-swarm-ingress:nginx-latest"
    ports: ConfigPorts = ConfigPorts()
    port_mode: PortPublishMode = PortPublishMode.ingress
    attach_to_host_network: bool = False
    replicas: Optional[int] = None
    service_mode: ServiceMode = ServiceMode.replicated
    preferences: List[ConfigPlacementPreference] = []  # FIXME
    maxreplicas: Optional[int] = 1
    networks: List[str] = []

    @property
    def endpoint_spec(self) -> Optional[EndpointSpec]:
        if self.attach_to_host_network:
            return None
        if self.port_mode == PortPublishMode.none:
            return None
        return EndpointSpec(
            ports={
                self.ports.http: (80, "tcp", self.port_mode.value),
                self.ports.https: (443, "tcp", self.port_mode.value),
            }
        )


class ConfigServiceRobot(ConfigServiceBase):
    name: str = "nginx-docker-ingress-robot"
    image: str = "gothack/docker-swarm-ingress:robot-latest"


class ConfigServices(BaseModel):
    account: ConfigServiceAccount = ConfigServiceAccount()
    challenge: ConfigServiceChallenge = ConfigServiceChallenge()
    nginx: ConfigServiceNginx = ConfigServiceNginx()
    robot: ConfigServiceRobot = ConfigServiceRobot()

    @root_validator
    def names_valid(cls, values):
        names_seen = set()
        for value in values.values():
            if value.name in names_seen:
                raise ValueError(f"Duplicate service name {value.name}")
            names_seen.add(value.name)
        return values


class ConfigRoot(BaseModel):
    acme: ConfigAcme
    services: ConfigServices = ConfigServices()


def config_load_and_convert(data: str) -> ConfigRoot:
    return ConfigRoot(**yaml.load(data, Loader=yaml.SafeLoader))


def main(argv):
    if len(argv) != 1:
        print("config.py [FILENAME_TO_VALIDATE]")

    with open(argv[0]) as f:
        config = config_load_and_convert(f.read())

    print("Config valid")

    print(repr(config))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
