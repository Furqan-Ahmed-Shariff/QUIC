import asyncio
import os
from collections import deque
from typing import cast
from urllib.parse import urlparse

import aioquic
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h0.connection import H0Connection
from aioquic.h3.connection import H3_ALPN, ErrorCode, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived

from aioquic.quic.configuration import QuicConfiguration

ca_certs = "pycacert.pem"
url = "https://localhost:4433/"


class HttpClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._http = None
        self._request_events = {}
        self._request_waiter = {}

        if self._quic.configuration.alpn_protocols[0].startswith("hq-"):
            self._http = H0Connection(self._quic)
        else:
            self._http = H3Connection(self._quic)

    async def get(self, url):
        """
        Perform a GET request.
        """
        return await self._request(method="GET", url=urlparse(url))

    def http_event_received(self, event):
        if isinstance(event, (HeadersReceived, DataReceived)):
            stream_id = event.stream_id
            if stream_id in self._request_events:
                # http
                self._request_events[stream_id].append(event)
                if event.stream_ended:
                    request_waiter = self._request_waiter.pop(stream_id)
                    request_waiter.set_result(self._request_events.pop(stream_id))

    def quic_event_received(self, event):
        #  pass event to the HTTP layer
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)

    async def _request(self, method, url):
        stream_id = self._quic.get_next_available_stream_id()
        full_path = url.path or "/"
        if url.query:
            self.full_path += "?" + url.query
        self._http.send_headers(
            stream_id=stream_id,
            headers=[
                (b":method", method.encode()),
                (b":scheme", url.scheme.encode()),
                (b":authority", url.netloc.encode()),
                (b":path", full_path.encode()),
                (b"user-agent", ("aioquic/" + aioquic.__version__).encode()),
            ],
            end_stream=True,
        )

        waiter = self._loop.create_future()
        self._request_events[stream_id] = deque()
        self._request_waiter[stream_id] = waiter
        self.transmit()

        return await asyncio.shield(waiter)


async def perform_http_request(client, url):
    # perform request
    http_events = await client.get(url)

    # print speed
    octets = 0
    for http_event in http_events:
        if isinstance(http_event, DataReceived):
            octets += len(http_event.data)

    # output response
    output_path = os.path.join(
        ".", os.path.basename(urlparse(url).path) or "index.html"
    )
    with open(output_path, "wb") as output_file:
        write_response(http_events=http_events, output_file=output_file)


def write_response(http_events, output_file):
    for http_event in http_events:
        if isinstance(http_event, DataReceived):
            output_file.write(http_event.data)


async def main(configuration, url, local_port):
    # parse URL
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port

    async with connect(
        host,
        port,
        configuration=configuration,
        create_protocol=HttpClient,
        local_port=local_port,
    ) as client:
        client = cast(HttpClient, client)
        # perform request
        await perform_http_request(
            client=client,
            url=url,
        )

        client._quic.close(error_code=ErrorCode.H3_NO_ERROR)


if __name__ == "__main__":
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=H3_ALPN,
    )

    configuration.load_verify_locations(ca_certs)

    asyncio.run(
        main(
            configuration=configuration,
            url=url,
            local_port=0,
        )
    )
