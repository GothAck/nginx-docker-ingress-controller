import re
import sys

from pydantic import BaseModel, validator
import yaml

RE_EMAIL = re.compile(r"^.+@.+$")


class ConfigPorts(BaseModel):
    http: int = 80
    https: int = 443

    @validator("http", "https")
    def ports_valid(cls, v: int, field):
        if v < 1 or v > 65535:
            raise ValueError("Invalid port")
        return v


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


class ConfigRoot(BaseModel):
    ports: ConfigPorts = ConfigPorts()
    acme: ConfigAcme


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
