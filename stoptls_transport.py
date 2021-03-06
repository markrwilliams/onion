from twisted.python.components import proxyForInterface
from twisted.internet.interfaces import ITCPTransport
from twisted.protocols.tls import TLSMemoryBIOFactory, TLSMemoryBIOProtocol
from twisted.protocols.policies import ProtocolWrapper, WrappingFactory


class PopOnDisconnectTransport(proxyForInterface(ITCPTransport)):
    """
    L{TLSMemoryBIOProtocol.loseConnection} shuts down the TLS session
    and calls its own transport's C{loseConnection}.  A zero-length
    read also calls the transport's C{loseConnection}.  This proxy
    uses that behavior to invoke a C{pop} callback when a session has
    ended.  The callback is invoked exactly once because
    C{loseConnection} must be idempotent.
    """
    def __init__(self, pop, **kwargs):
        super(PopOnDisconnectTransport, self).__init__(**kwargs)
        self._pop = pop

    def loseConnection(self):
        self._pop()
        self._pop = lambda: None


class OnionProtocol(ProtocolWrapper):
    """
    OnionProtocol is both a transport and a protocol.  As a protocol,
    it can run over any other ITransport.  As a transport, it
    implements stackable TLS.  That is, whatever application traffic
    is generated by the protocol running on top of OnionProtocol can
    be encapsulated in a TLS conversation.  Or, that TLS conversation
    can be encapsulated in another TLS conversation.  Or **that** TLS
    conversation can be encapsulated in yet *another* TLS
    conversation.

    Each layer of TLS can use different connection parameters, such as
    keys, ciphers, certificate requirements, etc.  At the remote end
    of this connection, each has to be decrypted separately, starting
    at the outermost and working in.  OnionProtocol can do this
    itself, of course, just as it can encrypt each layer starting with
    the innermost.
    """

    def __init__(self, *args, **kwargs):
        ProtocolWrapper.__init__(self, *args, **kwargs)
        # The application level protocol is the sentinel at the tail
        # of the linked list stack of protocol wrappers.  The stack
        # begins at this sentinel.
        self._tailProtocol = self._currentProtocol = self.wrappedProtocol


    def startTLS(self, contextFactory, client, bytes=None):
        """
        Add a layer of TLS, with SSL parameters defined by the given
        contextFactory.

        If *client* is True, this side of the connection will be an
        SSL client.  Otherwise it will be an SSL server.

        If extra bytes which may be (or almost certainly are) part of
        the SSL handshake were received by the protocol running on top
        of OnionProtocol, they must be passed here as the **bytes**
        parameter.
        """
        # The newest TLS session is spliced in between the previous
        # and the application protocol at the tail end of the list.
        tlsProtocol = TLSMemoryBIOProtocol(None, self._tailProtocol, False)
        tlsProtocol.factory = TLSMemoryBIOFactory(contextFactory, client, None)

        if self._currentProtocol is self._tailProtocol:
            # This is the first and thus outermost TLS session.  The
            # transport is the immutable sentinel that no startTLS or
            # stopTLS call will move within the linked list stack.
            # The wrappedProtocol will remain this outermost session
            # until it's terminated.
            self.wrappedProtocol = tlsProtocol
            nextTransport = PopOnDisconnectTransport(
                original=self.transport,
                pop=self._pop
            )
            # Store the proxied transport as the list's head sentinel
            # to enable an easy identity check in _pop.
            self._headTransport = nextTransport
        else:
            # This a later TLS session within the stack.  The previous
            # TLS session becomes its transport.
            nextTransport = PopOnDisconnectTransport(
                original=self._currentProtocol,
                pop=self._pop
            )

        # Splice the new TLS session into the linked list stack.
        # wrappedProtocol serves as the link, so the protocol at the
        # current position takes our new TLS session as its
        # wrappedProtocol.
        self._currentProtocol.wrappedProtocol = tlsProtocol
        # Move down one position in the linked list.
        self._currentProtocol = tlsProtocol
        # Expose the new, innermost TLS session as the transport to
        # the application protocol.
        self.transport = self._currentProtocol
        # Connect the new TLS session to the previous transport.  The
        # transport attribute also serves as the previous link.
        tlsProtocol.makeConnection(nextTransport)

        # Left over bytes are part of the latest handshake.  Pass them
        # on to the innermost TLS session.
        if bytes is not None:
            tlsProtocol.dataReceived(bytes)


    def stopTLS(self):
        self.transport.loseConnection()


    def _pop(self):
        pop = self._currentProtocol
        previous = pop.transport
        # If the previous link is the head sentinel, we've run out of
        # linked list.  Ensure that the application protocol, stored
        # as the tail sentinel, becomes the wrappedProtocol, and the
        # head sentinel, which is the underlying transport, becomes
        # the transport.
        if previous is self._headTransport:
            self._currentProtocol = self.wrappedProtocol = self._tailProtocol
            self.transport = previous
        else:
            # Splice out a protocol from the linked list stack.  The
            # previous transport is a PopOnDisconnectTransport proxy,
            # so first retrieve proxied object off its original
            # attribute.
            previousProtocol = previous.original
            # The previous protocol's next link becomes the popped
            # protocol's next link
            previousProtocol.wrappedProtocol = pop.wrappedProtocol
            # Move up one position in the linked list.
            self._currentProtocol = previousProtocol
            # Expose the new, innermost TLS session as the transport
            # to the application protocol.
            self.transport = self._currentProtocol



class OnionFactory(WrappingFactory):
    """
    A L{WrappingFactory} that overrides
    L{WrappingFactory.registerProtocol} and
    L{WrappingFactory.unregisterProtocol}.  These methods store in and
    remove from a dictionary L{ProtocolWrapper} instances.  The
    C{transport} patching done as part of the linked-list management
    above causes the instances' hash to change, because the
    C{__hash__} is proxied through to the wrapped transport.  They're
    not essential to this program, so the easiest solution is to make
    them do nothing.
    """
    protocol = OnionProtocol

    def registerProtocol(self, protocol):
        pass


    def unregisterProtocol(self, protocol):
        pass
