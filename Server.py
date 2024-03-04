import asyncio
import importlib
import aioquic
from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import HeadersReceived
from aioquic.h3.exceptions import NoAvailablePushIDError
from email.utils import formatdate
import time

certificate = "ssl_cert.pem"
private_key = "ssl_key.pem"
host = "::"
port = 4433

SERVER_NAME = "aioquic/" + aioquic.__version__


class MyHTTP3Server(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = None
        self.scope = None
        self.authority = None

    def http_event_received(self, event):
        if isinstance(event, HeadersReceived):
            authority = None
            headers = []
            http_version = "3"
            raw_path = b""
            method = ""
            protocol = None
            for header, value in event.headers:
                if header == b":authority":
                    authority = value
                    headers.append((b"host", value))
                elif header == b":method":
                    method = value.decode()
                elif header == b":path":
                    raw_path = value
                elif header == b":protocol":
                    protocol = value.decode()
                elif header and not header.startswith(b":"):
                    headers.append((header, value))

            if b"?" in raw_path:
                path_bytes, query_string = raw_path.split(b"?", maxsplit=1)
            else:
                path_bytes, query_string = raw_path, b""
            path = path_bytes.decode()
            self._quic._logger.info("HTTP request %s %s", method, path)

            # FIXME: add a public API to retrieve peer address
            client_addr = self._http._quic._network_paths[0].addr
            client = (client_addr[0], client_addr[1])

            extensions = {}
            if isinstance(self._http, H3Connection):
                extensions["http.response.push"] = {}
            self.scope = {
                "client": client,
                "extensions": extensions,
                "headers": headers,
                "http_version": http_version,
                "method": method,
                "path": path,
                "query_string": query_string,
                "raw_path": raw_path,
                "root_path": "",
                "scheme": "https",
                "type": "http",
            }
            self.authority = authority
            self.stream_id = event.stream_id
            asyncio.ensure_future(self.run_asgi(application))

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self._http = H3Connection(self._quic, enable_webtransport=True)

        #  pass event to the HTTP layer
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)

    async def run_asgi(self, app):
        await app(self.scope, self.receive, self.send)

    async def receive(self):
        pass

    async def send(self, message):
        if message["type"] == "http.response.start":
            self._http.send_headers(
                stream_id=self.stream_id,
                headers=[
                    (b":status", str(message["status"]).encode()),
                    (b"server", SERVER_NAME.encode()),
                    (b"date", formatdate(time.time(), usegmt=True).encode()),
                ]
                + [(k, v) for k, v in message["headers"]],
            )
        elif message["type"] == "http.response.body":
            self._http.send_data(
                stream_id=self.stream_id,
                data=message.get("body", b""),
                end_stream=not message.get("more_body", False),
            )
        elif message["type"] == "http.response.push" and isinstance(
            self._http, H3Connection
        ):
            request_headers = [
                (b":method", b"GET"),
                (b":scheme", b"https"),
                (b":authority", self.authority),
                (b":path", message["path"].encode()),
            ] + [(k, v) for k, v in message["headers"]]

            # send push promise
            try:
                push_stream_id = self._http.send_push_promise(
                    stream_id=self.stream_id, headers=request_headers
                )
            except NoAvailablePushIDError:
                return

        self.transmit()


async def main(host, port, configuration):
    server = await serve(
        host,
        port,
        configuration=configuration,
        create_protocol=MyHTTP3Server,
    )
    await asyncio.Future()


if __name__ == "__main__":
    module_str, attr_str = "ASGI_web_app.demo", "app"
    module = importlib.import_module(module_str)
    application = getattr(module, attr_str)

    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=False,
        max_datagram_frame_size=65536,
    )

    # load SSL certificate and key
    configuration.load_cert_chain(certificate, private_key)

    try:
        asyncio.run(
            main(
                host=host,
                port=port,
                configuration=configuration,
            )
        )
    except KeyboardInterrupt:
        pass
