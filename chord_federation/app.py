import chord_federation
import os
import tornado.gen
import tornado.ioloop
import tornado.web

from tornado.httpserver import HTTPServer
from tornado.netutil import bind_unix_socket
from tornado.web import RequestHandler, url

from .constants import *
from .db import peer_db
from .peers import PeerManager, PeerHandler
from .search import SearchHandler


# noinspection PyAbstractClass,PyAttributeOutsideInit
class ServiceInfoHandler(RequestHandler):
    async def get(self):
        # Spec: https://github.com/ga4gh-discovery/ga4gh-service-info

        if self.get_argument("update_peers", "true") == "true":
            # Hack to force lists to update when the CHORD dashboard is loaded
            c = self.application.db.cursor()
            await self.application.peer_manager.get_peers(c)
            self.application.db.commit()

        self.write({
            "id": "ca.distributedgenomics.chord_federation",  # TODO: Should be globally unique
            "name": "CHORD Federation",  # TODO: Should be globally unique
            "type": "ca.distributedgenomics:chord_federation:{}".format(chord_federation.__version__),  # TODO
            "description": "Federation service for a CHORD application.",
            "organization": {
                "name": "GenAP",
                "url": "https://genap.ca/"
            },
            "contactUrl": "mailto:david.lougheed@mail.mcgill.ca",
            "version": chord_federation.__version__
        })


class Application(tornado.web.Application):
    def __init__(self, db, base_url):
        self.db = db
        self.peer_manager = PeerManager()

        handlers = [
            url(f"{base_url}/service-info", ServiceInfoHandler),
            url(f"{base_url}/peers", PeerHandler),
            url(f"{base_url}/search-aggregate/([a-zA-Z0-9\\-_/]+)", SearchHandler),
        ]

        super(Application, self).__init__(handlers)


application = Application(peer_db, os.environ.get("BASE_URL", ""))


def run():
    if CHORD_URL is None or CHORD_URL == "":
        print("[CHORD Federation] No CHORD URL given, terminating...")
        exit(1)

    server = HTTPServer(application)
    server.add_socket(bind_unix_socket(os.environ.get("SOCKET", "/tmp/federation.sock")))
    tornado.ioloop.IOLoop.instance().start()