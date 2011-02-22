
from twisted.python.log import startLogging
from twisted.internet import reactor
from twisted.internet.stdio import StandardIO
from twisted.protocols.basic import LineReceiver
from twisted.internet.ssl import ClientContextFactory
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.protocol import Factory

from stoptls_server import OnionFactory

class Sender(LineReceiver):
    from os import linesep as delimiter

    def __init__(self, client):
        self.client = client


    def lineReceived(self, line):
        self.client.sendLine(line)



class OnionClient(LineReceiver):
    def connectionMade(self):
        StandardIO(Sender(self))


    def connectionLost(self, reason):
        print 'Connection lost'


    def sendLine(self, line):
        LineReceiver.sendLine(self, line)
        if line == "secure":
            self.transport.startTLS(self.factory.contextFactory, True)
        elif line == "unsecure":
            self.transport.stopTLS()


    def lineReceived(self, line):
        print 'Received:', repr(line)



def main():
    startLogging(file('client.log', 'a'), False)
    factory = Factory()
    factory.contextFactory = ClientContextFactory()
    factory.protocol = OnionClient

    endpoint = TCP4ClientEndpoint(reactor, "127.0.0.1", 9533)
    endpoint.connect(OnionFactory(factory))
    reactor.run()

if __name__ == '__main__':
    main()
