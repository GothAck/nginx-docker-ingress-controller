import re
import sys
from typing import List, Optional, Tuple
from docker.types.services import EndpointSpec

from pydantic import BaseModel, validator
import yaml

RE_EMAIL = re.compile(r"^.+@.+$")


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

    @property
    def endpoint_spec(self) -> Optional[EndpointSpec]:
        return None


class ConfigServiceAccount(ConfigServiceBase):
    name: str = "nginx-docker-ingress-account"
    image: str = "gothack/docker-swarm-ingress:robot-latest"


class ConfigServiceChallenge(ConfigServiceBase):
    name: str = "nginx-docker-ingress-challenge"
    image: str = "gothack/docker-swarm-ingress:challenge-latest"

    @property
    def endpoint_spec(self) -> Optional[EndpointSpec]:
        return None  # FIXME


class ConfigServiceNginx(ConfigServiceBase):
    name: str = "nginx-docker-ingress-nginx"
    image: str = "gothack/docker-swarm-ingress:nginx-latest"
    ports: ConfigPorts = ConfigPorts()
    replicas: int = 1
    preferences: List[ConfigPlacementPreference] = []  # FIXME
    maxreplicas: Optional[int] = None

    @property
    def endpoint_spec(self) -> Optional[EndpointSpec]:
        return EndpointSpec(ports={self.ports.http: 80, self.ports.https: 443})


class ConfigServiceRobot(ConfigServiceBase):
    name: str = "nginx-docker-ingress-robot"
    image: str = "gothack/docker-swarm-ingress:robot-latest"


class ConfigServices(BaseModel):
    account: ConfigServiceAccount = ConfigServiceAccount()
    challenge: ConfigServiceChallenge = ConfigServiceChallenge()
    nginx: ConfigServiceNginx = ConfigServiceNginx()
    robot: ConfigServiceRobot = ConfigServiceRobot()


class ConfigRoot(BaseModel):
    acme: ConfigAcme
    services: ConfigServices


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
