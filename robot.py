from typing import List, Optional

import asyncio
import logging
import sys

import docker

from common import DockerAdapter, ServiceAdapter
from robocert import RoboCert

logging.basicConfig()
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("robot")
logger.setLevel(logging.DEBUG)


class Robot:
    adapter: DockerAdapter
    cert: RoboCert

    def __init__(self) -> None:
        self.adapter = DockerAdapter(docker.from_env())
        self.cert = RoboCert()

    async def begin(self):
        await self.cert.begin()

    async def ensure_account(self) -> bool:
        if await self.cert.load_account():
            return True
        return await self.cert.create_account(
            "docker-swarm-ingress@greg.gothack.ninja", True
        )

    @staticmethod
    def service_needs(service: ServiceAdapter) -> Optional[str]:
        if not service.latest_cert_pair:
            return "new"
        elif service.cert_renewable:
            return "renew"
        return None

    async def observe(self) -> None:
        logger.info("Observe")
        services = list(
            filter(
                lambda sp: sp[1] is not None,
                (
                    (service, self.service_needs(service))
                    for service in self.adapter.services
                    if service.acme_ssl
                ),
            )
        )

        logger.info("Services requiring updates %r", services)

        service_futs = [
            asyncio.create_task(self.cert.order_cert(service[0]))
            for service in services
        ]

        if service_futs:
            await asyncio.wait(service_futs)

        logger.info("Observe done")

        # TODO: Clean up old keys and certs here

    async def observe_and_obey(self) -> None:
        if not await self.cert.load_account():
            logger.critical("Could not load account")
            return

        while True:
            await self.observe()
            await asyncio.sleep(10)


async def main_ensure_account(robot: Robot) -> int:
    logger.info("Robot will ensure account exists")
    if await robot.ensure_account():
        return 0
    return 1


async def main_observe_and_obey(robot: Robot) -> int:
    logger.info("Robot will observe and obey")

    await robot.observe_and_obey()

    return 0


async def main(argv: List[str]) -> int:
    logger.info("Booting Nginx docker ingress robot")
    robot = Robot()

    await robot.begin()

    if argv[0] == "ensure-account":
        return await main_ensure_account(robot)
    elif argv[0] == "observe-and-obey":
        return await main_observe_and_obey(robot)

    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
