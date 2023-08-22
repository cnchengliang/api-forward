import asyncio
import time
import traceback
import uuid
from itertools import cycle
from typing import Any, AsyncGenerator, List

import anyio
import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger

from ..helper import async_retry
from .settings import *


class ForwardingBase:
    BASE_URL = None
    ROUTE_PREFIX = None
    client: httpx.AsyncClient = None

    if IP_BLACKLIST or IP_WHITELIST:
        validate_host = True
    else:
        validate_host = False

    timeout = TIMEOUT

    @staticmethod
    def validate_request_host(ip):
        """
        Validates the request host IP address against the IP whitelist and blacklist.

        Args:
            ip (str): The IP address to be validated.

        Raises:
            HTTPException: If the IP address is not in the whitelist or if it is in the blacklist.
        """
        if IP_WHITELIST and ip not in IP_WHITELIST:
            logger.warning(f"IP {ip} is not in the whitelist")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden Error",
            )
        if IP_BLACKLIST and ip in IP_BLACKLIST:
            logger.warning(f"IP {ip} is in the blacklist")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden Error",
            )

    @staticmethod
    async def aiter_bytes(r: httpx.Response) -> AsyncGenerator[bytes, Any]:
        async for chunk in r.aiter_bytes():
            yield chunk
        await r.aclose()

    @async_retry(
        max_retries=3,
        delay=0.5,
        backoff=2,
        exceptions=(HTTPException, anyio.EndOfStream),
    )
    async def try_send(self, client_config: dict, request: Request):
        """
        Try to send the request.

        Args:
            client_config (dict): The configuration for the client.
            request (Request): The request to be sent.

        Returns:
            Response: The response from the client.

        Raises:
            HTTPException: If there is a connection error or any other exception occurs.
        """
        try:
            req = self.client.build_request(
                method=request.method,
                url=client_config['url'],
                headers=client_config["headers"],
                content=request.stream(),
                timeout=self.timeout,
            )
            print(req)
            return await self.client.send(req, stream=True)

        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            error_info = (
                f"{type(e)}: {e} | "
                f"Please check if host={request.client.host} can access [{self.BASE_URL}] successfully?"
            )
            traceback_info = traceback.format_exc()
            logger.error(f"{error_info} traceback={traceback_info}")
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=error_info
            )

        except anyio.EndOfStream:
            error_info = "EndOfStream Error: trying to read from a stream that has been closed from the other end."
            traceback_info = traceback.format_exc()
            logger.error(f"{error_info} traceback={traceback_info}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_info
            )

        except Exception as e:
            error_info = f"{type(e)}: {e}"
            traceback_info = traceback.format_exc()
            logger.error(f"{error_info} traceback={traceback_info}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_info
            )

    def prepare_client(self, request: Request):
        """
        Prepares the client configuration based on the given request.

        Args:
            request (Request): The request object containing the necessary information.

        Returns:
            dict: The client configuration dictionary with the necessary parameters set.
                  The dictionary has the following keys:
                  - 'auth': The authorization header value.
                  - 'headers': The dictionary of headers.
                  - 'url': The URL object.
                  - 'url_path': The URL path.

        Raises:
            AssertionError: If the `BASE_URL` or `ROUTE_PREFIX` is not set.
        """
        assert self.BASE_URL is not None
        assert self.ROUTE_PREFIX is not None
        if self.validate_host:
            ip = get_client_ip(request)
            # print(f"{ip=}")
            self.validate_request_host(ip)

        _url_path = request.url.path
        prefix_index = 0 if self.ROUTE_PREFIX == '/' else len(self.ROUTE_PREFIX)

        url_path = _url_path[prefix_index:]
        url = httpx.URL(path=url_path, query=request.url.query.encode("utf-8"))
        headers = dict(request.headers)
        auth = headers.pop("authorization", "")
        content_type = headers.pop("content-type", "application/json")
        auth_headers_dict = {"Content-Type": content_type, "Authorization": auth}
        client_config = {
            'auth': auth,
            'headers': auth_headers_dict,
            'url': url,
            'url_path': url_path,
        }

        return client_config

    async def reverse_proxy(self, request: Request):
        """
        Reverse proxies the given request.

        Args:
            request (Request): The request to be reverse proxied.

        Returns:
            StreamingResponse: The response from the reverse proxied server, as a streaming response.
        """
        assert self.client is not None

        client_config = self.prepare_client(request)

        r = await self.try_send(client_config, request)

        return StreamingResponse(
            self.aiter_bytes(r),
            status_code=r.status_code,
            media_type=r.headers.get("content-type"),
        )