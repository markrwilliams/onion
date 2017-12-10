_This is an answer to [an old StackOverflow question](https://stackoverflow.com/questions/5130080/why-is-there-a-handshake-failure-when-trying-to-run-tls-over-tls-with-this-code/47728193#47728193)._

There are at least two problems with [`OnionProtocol`](https://github.com/markrwilliams/onion/blob/6e021eefba5845cb5a8d5b3cb799e417fa707093/stoptls_transport.py#L22-L50):

1. The _innermost_ `TLSMemoryBIOProtocol` becomes the `wrappedProtocol`, when it should be the _outermost_;
2. `ProtocolWithoutConnectionLost` does not pop any `TLSMemoryBIOProtocol`s off `OnionProtocol`'s stack, because `connectionLost` is only called after a `FileDescriptor`s `doRead` or `doWrite` methods return a reason for disconnection.

We can't solve the first problem without changing the way `OnionProtocol` manages its stack, and we can't solve the second until we figure out the new stack implementation.  Unsurprisingly, the correct design is a direct consequence of how data flows within Twisted, so we'll start with some data flow analysis.

Twisted represents an established connection with an instance of either [`twisted.internet.tcp.Server`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/internet/tcp.py#L744-L852) or [`twisted.internet.tcp.Client`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/internet/tcp.py#L735-L740). Since the only interactivity in our program happens in [`stoptls_client`](https://github.com/markrwilliams/onion/blob/6e021eefba5845cb5a8d5b3cb799e417fa707093/stoptls_client.py), we'll only consider the data flow to and from a `Client` instance.

Let's warm up with a minimal `LineReceiver` client that echoes back lines received from a local server on port 9999:

    from twisted.protocols import basic
    from twisted.internet import defer, endpoints, protocol, task
    
    class LineReceiver(basic.LineReceiver):
        def lineReceived(self, line):
            self.sendLine(line)
    
    def main(reactor):
        clientEndpoint = endpoints.clientFromString(
            reactor, "tcp:localhost:9999")
        connected = clientEndpoint.connect(
            protocol.ClientFactory.forProtocol(LineReceiver))
        def waitForever(_):
            return defer.Deferred()
        return connected.addCallback(waitForever)
    
    task.react(main)

Once the established connection's established, a `Client` becomes our `LineReceiver` protocol's transport and mediates input and output: 

[![Client and LineReceiver][1]][1]

New data from the server causes the reactor to call the `Client`'s `doRead` method, which in turn passes what it's received to `LineReceiver`'s `dataReceived` method. Finally, `LineReceiver.dataReceived` calls `LineReceiver.lineReceived` when at least one line is available.

Our application sends a line of data back to the server by calling `LineReceiver.sendLine`.  This calls `write` on the transport bound to the protocol instance, which is the same `Client` instance that handled incoming data.  `Client.write` arranges for the data to be sent by the reactor, while `Client.doWrite` actually sends the data over the socket.

We're ready to look at the behaviors of an [`OnionClient`](https://github.com/markrwilliams/onion/blob/6e021eefba5845cb5a8d5b3cb799e417fa707093/stoptls_client.py#L25-L39) that never calls `startTLS`:

[![OnionClient without startTLS][2]][2]

`OnionClient`s are wrapped in [`OnionProtocol`s](https://github.com/markrwilliams/onion/blob/6e021eefba5845cb5a8d5b3cb799e417fa707093/stoptls_transport.py#L22-L50), which are the crux of our attempt at nested TLS.  As a subclass of [`twisted.internet.policies.ProtocolWrapper`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/policies.py#L39), an instance of `OnionProtocol` is a kind of protocol-transport sandwich; it presents itself as a _protocol_ to a lower-level transport and as a _transport_ to a protocol it wraps through a masquerade established at connection time by a [`WrappingFactory`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/policies.py#L129).  

Now, `Client.doRead` calls `OnionProtocol.dataReceived`, which proxies the data through to `OnionClient`.  As `OnionClient`'s transport, `OnionProtocol.write` accepts lines to send from `OnionClient.sendLine` and proxies them down to `Client`, its _own_ transport.  This is the normal interaction between a `ProtocolWrapper`, its wrapped protocol, and its own transport, so naturally data flows to and from each without any trouble.

`OnionProtocol.startTLS` does something different.  It attempts to interpose a new `ProtocolWrapper` — which happens to be a [`TLSMemoryBIOProtocol`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L115) — between an _established_ protocol-transport pair.  This seems easy enough: a `ProtocolWrapper` stores the upper-level protocol as its [`wrappedProtocol` attribute](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/policies.py#L54), and [proxies `write` and other attributes down to its own transport](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/policies.py#L113-L114).  `startTLS` should be able to inject a new `TLSMemoryBIOProtocol` that wraps `OnionClient` into the connection by patching that instance over its own `wrappedProtocol` and `transport`:

    def startTLS(self):
        ...
        connLost = ProtocolWithoutConnectionLost(self.wrappedProtocol)
        connLost.onion = self
        # Construct a new TLS layer, delivering events and application data to the
        # wrapper just created.
        tlsProtocol = TLSMemoryBIOProtocol(None, connLost, False)
    
        # Push the previous transport and protocol onto the stack so they can be
        # retrieved when this new TLS layer stops.
        self._tlsStack.append((self.transport, self.wrappedProtocol))
        ...
        # Make the new TLS layer the current protocol and transport.
        self.wrappedProtocol = self.transport = tlsProtocol

Here's the flow of data after the first call to `startTLS`:

[![startTLS one TLSMemoryBIOProtocol, working][3]][3]

As expected, new data delivered to `OnionProtocol.dataReceived` is routed to the `TLSMemoryBIOProtocol` stored on the `_tlsStack`, which passes the decrypted plaintext to `OnionClient.dataReceived`.  `OnionClient.sendLine` also passes its data to `TLSMemoryBIOProtocol.write`, which encrypts it and sends the resulting ciphertext to `OnionProtocol.write` and then `Client.write`.

Unfortunately this scheme fails after a second call to `startTLS`.  The root cause is this line:

        self.wrappedProtocol = self.transport = tlsProtocol

Each call to `startTLS` replaces the `wrappedProtocol` with the _innermost_ `TLSMemoryBIOProtocol`, even though the data received by `Client.doRead` was encrypted by the _outermost_:

[![startTLS two TLSMemoryBIOProtocols, broken][4]][4]

The `transport`s, however, are nested correctly.  `OnionClient.sendLine` can only call its transport's `write` — that is, `OnionProtocol.write` — so `OnionProtocol` should replace its `transport` with the innermost `TLSMemoryBIOProtocol` to ensure writes are successively nested inside additional layers of encryption.

The solution, then, is to ensure that data flows through the _first_ `TLSMemoryBIOProtocol` on the `_tlsStack` to the _next_ one in turn, so that each layer of encryption is peeled off in the reverse order it was applied:

[![startTLS with two TLSMemoryBIOProtocols, working][5]][5]

Representing `_tlsStack` as a list seems less natural given this new requirement.  Fortunately, representing the incoming data flow linearly suggests a new data structure:

[![Incoming data as a linked list traversal][6]][6]

Both the buggy and correct flow of incoming data resemble a singly-linked list, with `wrappedProtocol` serving as `ProtocolWrapper`s next links and `protocol` serving as `Client`'s.  The list should grow downward from `OnionProtocol` and always end with `OnionClient`.  The bug occurs because that ordering invariant is violated.

A singly-linked list is fine for pushing protocols onto the stack but awkward for popping them off, because it requires traversal downwards from its head to the node to remove.  Of course, this traversal happens every time data's received, so the concern is the complexity implied by an additional traversal rather than worst-case time complexity.  Fortunately, the list is actually doubly linked:

[![Doubly linked list with protocols and transports][7]][7]

The `transport` attribute links each nested protocol with its predecessor, so that `transport.write` can layer on successively lower levels of encryption before finally sending the data across the network.  We have two sentinels to aid in managing the list: `Client` must always be at the top and `OnionClient` must always be at the bottom.

Putting the two together, we end up with this:

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


(This is also available on [GitHub](https://github.com/markrwilliams/onion/blob/6f16e3b15eabee8e0043f1e2034ca7c0099d1163/stoptls_transport.py).)

The solution to the second problem lies in `PopOnDisconnectTransport`.  The original code attempted to pop off a TLS session from the stack via `connectionLost`, but because only a closed file descriptor causes `connectionLost` to be called, it failed to remove stopped TLS sessions that didn't close the underlying socket.

At the time of this writing, `TLSMemoryBIOProtocol` calls its transport's `loseConnection` in exactly two places: [`_shutdownTLS`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L351) and [`_tlsShutdownFinished`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L385).  `_shutdownTLS` is called on active closes ([`loseConnection`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L422), [`abortConnection`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L432), [`unregisterProducer`](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L620) and [after `loseConnection` and all pending writes have been flushed](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L502)), while `_tlsShutdownFinished` is called on passive closes ([handshake failures](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L239), [empty reads](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L285), [read errors](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L292), and [write errors](https://github.com/twisted/twisted/blob/twisted-17.9.0/src/twisted/protocols/tls.py#L536)).  This all means that _both_ sides of a closed connection can pop stopped TLS sessions off the stack during `loseConnection`. `PopOnDisconnectTransport` does this idempotently because `loseConnection` is generally idempotent, and `TLSMemoryBIOProtocol` certainly expects it to be.

The downside to putting stack management logic in `loseConnection` is that it depends on the particulars of `TLSMemoryBIOProtocol`'s implementation.  A generalized solution would require new APIs across many levels of Twisted.

Until then, we're stuck with another example of [Hyrum's Law](http://www.hyrumslaw.com/).

  [1]: images/lineReceiver.png
  [2]: images/onionProtocol.png
  [3]: images/onionProtocolTLS0.png
  [4]: images/onionProtocolTLS0TLS1Bug.png
  [5]: images/onionProtocolTLS0TLS1Works.png
  [6]: images/receiveLinkedList.png
  [7]: images/linkedLists.png
