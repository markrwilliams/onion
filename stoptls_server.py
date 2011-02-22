from sys import stdout, argv

from OpenSSL.SSL import FILETYPE_PEM

from twisted.python.log import startLogging
from twisted.python.filepath import FilePath
from twisted.internet import reactor
from twisted.internet.ssl import KeyPair, PrivateCertificate
from twisted.protocols.basic import LineReceiver
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import serverFromString

from stoptls_transport import OnionFactory

class SomeProtocol(LineReceiver):
    def rawDataReceived(self, bytes):
        self.transport.startTLS(self.factory.contextFactory, False, bytes)
        self.setLineMode()


    def lineReceived(self, line):
        if line == "secure":
            self.setRawMode()
        elif line == "unsecure":
            self.transport.stopTLS()
        else:
            self.sendLine('Echo: %s' % (line,))



def main():
    startLogging(stdout, False)
    keyPath = FilePath(argv[1])
    certPath = FilePath(argv[2])
    key = KeyPair.load(keyPath.getContent(), FILETYPE_PEM)
    cert = PrivateCertificate.load(certPath.getContent(), key, FILETYPE_PEM)

    factory = Factory()
    factory.contextFactory = cert.options()
    factory.protocol = SomeProtocol
    endpoint = serverFromString(reactor, argv[3])
    endpoint.listen(OnionFactory(factory))
    reactor.run()


if __name__ == '__main__':
    main()
