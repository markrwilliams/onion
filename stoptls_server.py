from sys import stdout

from OpenSSL.SSL import FILETYPE_PEM

from twisted.python.log import startLogging
from twisted.python.filepath import FilePath
from twisted.internet import reactor
from twisted.internet.error import ConnectionDone
from twisted.internet.ssl import KeyPair, PrivateCertificate
from twisted.protocols.basic import LineReceiver
from twisted.python.components import proxyForInterface
from twisted.internet.interfaces import ITCPTransport, IProtocol
from twisted.internet.protocol import Factory
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.protocols.tls import TLSMemoryBIOFactory, TLSMemoryBIOProtocol
from twisted.protocols.policies import ProtocolWrapper, WrappingFactory


class TransportWithoutDisconnection(proxyForInterface(ITCPTransport)):
    def loseConnection(self):
        print 'lose connection now'


class ProtocolWithoutConnectionLost(proxyForInterface(IProtocol)):
    def connectionLost(self, reason):
        if reason.check(ConnectionDone):
            self.onion._stopped()
        else:
            super(ProtocolWithoutConnectionLost, self).connectionLost(reason)



class OnionProtocol(ProtocolWrapper):
    def startTLS(self, contextFactory, client, bytes=None):
        connLost = ProtocolWithoutConnectionLost(self.wrappedProtocol)
        connLost.onion = self
        tlsProtocol = TLSMemoryBIOProtocol(None, connLost, False)
        tlsProtocol.factory = TLSMemoryBIOFactory(contextFactory, client, None)

        self._oldTransport = self.transport
        self.transport = tlsProtocol
        self.transport.makeConnection(TransportWithoutDisconnection(self._oldTransport))

        self._oldWrappedProtocol = self.wrappedProtocol
        self.wrappedProtocol = self.transport
        if bytes is not None:
            self.wrappedProtocol.dataReceived(bytes)


    def stopTLS(self):
        self.transport.loseConnection()


    def _stopped(self):
        self.transport = self._oldTransport
        self.wrappedProtocol = self._oldWrappedProtocol



class OnionFactory(WrappingFactory):
    protocol = OnionProtocol



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
            self.sendLine('Echo: %r' % (line,))



def main():
    startLogging(stdout, False)
    keyPath = FilePath("server.pem")
    certPath = keyPath
    key = KeyPair.load(keyPath.getContent(), FILETYPE_PEM)
    cert = PrivateCertificate.load(certPath.getContent(), key, FILETYPE_PEM)

    factory = Factory()
    factory.contextFactory = cert.options()
    factory.protocol = SomeProtocol
    endpoint = TCP4ServerEndpoint(reactor, 9533)
    endpoint.listen(OnionFactory(factory))
    reactor.run()


from twisted.python.failure import startDebugMode
startDebugMode()

if __name__ == '__main__':
    main()
