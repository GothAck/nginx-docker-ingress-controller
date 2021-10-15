from typing import List

import asyncio
import logging
import sys
import base64

from aiohttp import web
import docker

from common import CONFIG_CHALLENGE_BASE, DockerAdapter

logging.basicConfig()
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("challenge")
logger.setLevel(logging.DEBUG)


class Challenge:
    adapter: DockerAdapter

    def __init__(self) -> None:
        self.adapter = DockerAdapter(docker.from_env())
        self.http = web.Application()

        self.http.router.add_get("/.well-known/acme-challenge/{token}", self.handler)

    def get_challenge(self, token):
        config = self.adapter.config_read(f"{CONFIG_CHALLENGE_BASE}.{token}")
        if config is None:
            raise web.HTTPNotFound()
        response = base64.b64decode(config.attrs["Spec"]["Data"]).decode("utf-8")
        return response

    async def handler(self, req: web.Request) -> web.Response:
        token = req.match_info["token"]

        logger.info("handler %s", token)

        return web.Response(text=self.get_challenge(token))

    async def run(self) -> None:
        await asyncio.wait(
            [asyncio.create_task(web._run_app(self.http, port=80, print=logger.info))]
        )


async def main(argv: List[str]) -> int:
    logger.info("Booting Nginx docker ingress challenge handler")
    challenge = Challenge()

    await challenge.run()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
