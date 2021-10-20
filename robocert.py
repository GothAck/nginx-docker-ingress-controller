from typing import Dict, List, cast

import datetime
import logging
import time

from acme import crypto_util
import OpenSSL

from acmeasync.acmele import ACMELE, Challenge
import docker

from common import (
    CONFIG_CHALLENGE_BASE,
    SECRET_SVC_BASE,
    DockerAdapter,
    SECRET_ACME_ACCOUNT,
    ServiceAdapter,
)

logger = logging.getLogger(__name__)

DIRECTORY_URI = "https://acme-v02.api.letsencrypt.org/directory"
ACCOUNT_SECRET_PATH = f"/run/secrets/{SECRET_ACME_ACCOUNT}"


class RoboCert:
    def __init__(self) -> None:
        self.adapter = DockerAdapter(docker.from_env())
        self.acme = ACMELE(DIRECTORY_URI)

    async def begin(self) -> None:
        await self.acme.begin()

    async def load_account(self) -> bool:
        return await self.acme.loadAccount(ACCOUNT_SECRET_PATH)

    async def create_account(self, email: str, tos: bool) -> bool:
        if await self.acme.createAccount(email, tos):
            self.adapter.write_secret(
                SECRET_ACME_ACCOUNT, await self.acme.saveAccountData()
            )
            return True
        return False

    async def order_cert(self, service: ServiceAdapter) -> bool:
        logger.info("Order cert")
        order = await self.acme.createOrder(service.hosts)

        latest_cert_version = service.latest_cert_version
        next_cert_version = (
            0 if latest_cert_version is None else latest_cert_version + 1
        )

        if not order:
            logger.critical("Failed to create order")
            return False

        challs: List[Challenge] = []
        for auth in await order.authorizations():
            for chall in await auth.challenges("http-01"):
                token = chall.data["token"]
                self.adapter.config_write(
                    f"{CONFIG_CHALLENGE_BASE}.{token}",
                    f"{token}.{self.acme.account_key_thumbprint}",
                )
                challs.append(await chall.begin())

        if not challs:
            logger.exception("No http-01 challenges")
            return False

        logger.info("Awaiting challenges")

        for chall in challs:
            await chall.await_status("valid")

        for chall in challs:
            # FIXME: Clean up challenges
            pass

        logger.info("Awaiting order status")

        await order.await_not_status("pending")

        if order.status != "ready":
            logger.critical(
                "Order is in invalid state %s expecting ready", order.status
            )
            return False

        pkey = OpenSSL.crypto.PKey()
        pkey.generate_key(OpenSSL.crypto.TYPE_RSA, 2048)
        key_pem = cast(
            bytes, OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, pkey)
        )

        csr_pem = crypto_util.make_csr(key_pem, service.hosts)

        logger.info("Finalizing order")

        await order.finalize(csr_pem)

        logger.info("Awaiting finalization")

        await order.await_status("valid")

        logger.info("Finalized")

        cert_pem = await order.get_cert()

        cert_obj = OpenSSL.crypto.load_certificate(
            OpenSSL.crypto.FILETYPE_PEM, cert_pem.encode("utf-8")
        )

        cert_expiry = datetime.datetime.strptime(
            cert_obj.get_notAfter().decode("ascii"), "%Y%m%d%H%M%SZ"
        )
        cert_expiry_unix = time.mktime(cert_expiry.timetuple())

        logger.info("Writing secrets")

        key_secret_name = (
            f"{SECRET_SVC_BASE}.{service.model.id}.key.{next_cert_version}"
        )
        cert_secret_name = (
            f"{SECRET_SVC_BASE}.{service.model.id}.crt.{next_cert_version}"
        )

        key_secret = self.adapter.read_secret(key_secret_name)
        if key_secret:
            key_secret.remove()

        cert_secret = self.adapter.read_secret(cert_secret_name)
        if cert_secret:
            cert_secret.remove()

        self.adapter.write_secret(key_secret_name, key_pem.decode("utf-8"))
        self.adapter.write_secret(
            cert_secret_name, cert_pem, labels=dict(expires=str(cert_expiry_unix))
        )

        logger.info("Order complete")

        return True
