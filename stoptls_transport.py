from twisted.python.components import proxyForInterface
from twisted.internet.error import ConnectionDone
from twisted.internet.interfaces import ITCPTransport, IProtocol
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
    def makeConnection(self, transport):
        self._tlsStack = []
        ProtocolWrapper.makeConnection(self, transport)


    def startTLS(self, contextFactory, client, bytes=None):
        connLost = ProtocolWithoutConnectionLost(self.wrappedProtocol)
        connLost.onion = self
        tlsProtocol = TLSMemoryBIOProtocol(None, connLost, False)
        tlsProtocol.factory = TLSMemoryBIOFactory(contextFactory, client, None)

        self._tlsStack.append((self.transport, self.wrappedProtocol))

        transport = TransportWithoutDisconnection(self.transport)
        self.transport = tlsProtocol
        self.transport.makeConnection(transport)

        self.wrappedProtocol = self.transport
        if bytes is not None:
            self.wrappedProtocol.dataReceived(bytes)


    def stopTLS(self):
        self.transport.loseConnection()


    def _stopped(self):
        self.transport, self.wrappedProtocol = self._tlsStack.pop()



class OnionFactory(WrappingFactory):
    protocol = OnionProtocol



