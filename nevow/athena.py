import itertools, os

from zope.interface import implements

from twisted.internet import defer, error, reactor
from twisted.python import log

from nevow import inevow, rend, url, static, json, util, tags, guard

class LivePageError(Exception):
    """base exception for livepage errors"""

def neverEverCache(request):
    """
    Set headers to indicate that the response to this request should
    never, ever be cached.
    """
    request.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate')
    request.setHeader('Pragma', 'no-cache')

def activeChannel(request):
    """
    Mark this connection as a 'live' channel by setting the
    Connection: close header and flushing all headers immediately.
    """
    request.setHeader("Connection", "close")
    request.write('')

class LivePageTransport(object):
    implements(inevow.IResource)

    def __init__(self, livePage):
        self.livePage = livePage

    def locateChild(self, ctx, segments):
        return rend.NotFound

    def renderHTTP(self, ctx):
        req = inevow.IRequest(ctx)
        try:
            d = self.livePage.addTransport(req)
        except defer.QueueOverflow:
            log.msg("Fast transport-close path")
            d = defer.succeed('')
        args = req.args.get('args', [()])[0]
        if args != ():
            args = json.parse(args)
        kwargs = req.args.get('kw', [{}])[0]
        if kwargs != {}:
            args = json.parse(kwargs)
        method = getattr(self, 'action_' + req.args['action'][0])
        method(ctx, *args, **kwargs)
        return d

    def _cbCall(self, result, requestId):
        def cb((d, req)):
            d.callback((None, unicode(requestId), u'text/json', result))
        self.livePage.getTransport().addCallback(cb)

    def _ebCall(self, err, method, func):
        log.msg("Dispatching %r to %r failed unexpectedly:" % (method, func))
        log.err(err)
        return err

    def action_call(self, ctx, method, *args, **kw):
        func = self.livePage.locateMethod(ctx, method)
        requestId = inevow.IRequest(ctx).getHeader('Request-Id')
        if requestId is not None:
            result = defer.maybeDeferred(func, ctx, *args, **kw)
            result.addErrback(self._ebCall, method, func)
            result.addBoth(self._cbCall, requestId)
        else:
            try:
                func(ctx, *args, **kw)
            except:
                log.msg("Unhandled error in event handler:")
                log.err()

    def action_respond(self, ctx, *args, **kw):
        responseId = inevow.IRequest(ctx).getHeader('Response-Id')
        if responseId is None:
            log.msg("No Response-Id given")
            return

        self.livePage._remoteCalls.pop(responseId).callback((args, kw))

    def action_noop(self, ctx):
        pass


class LivePageFactory:
    noisy = True

    def __init__(self, pageFactory):
        self._pageFactory = pageFactory
        self.clients = {}

    def clientFactory(self, context):
        livepageId = inevow.IRequest(context).getHeader('Livepage-Id')
        if livepageId is not None:
            livepage = self.clients.get(livepageId)
            if livepage is not None:
                # A returning, known client.  Give them their page.
                return livepage
            else:
                # A really old, expired client.  Or maybe an evil
                # hax0r.  Give them a fresh new page and log the
                # occurrence.
                if self.noisy:
                    log.msg("Unknown Livepage-Id: %r" % (livepageId,))
                return self._manufactureClient()
        else:
            # A brand new client.  Give them a brand new page!
            return self._manufactureClient()

    def _manufactureClient(self):
        cl = self._pageFactory()
        cl.factory = self
        return cl

    def addClient(self, client):
        id = self._newClientID()
        self.clients[id] = client
        if self.noisy:
            log.msg("Rendered new LivePage %r: %r" % (client, id))
        return id

    def removeClient(self, clientID):
        del self.clients[clientID]
        if self.noisy:
            log.msg("Disconnected old LivePage %r" % (clientID,))

    def _newClientID(self):
        return guard._sessionCookie()

    def getClients(self):
        return self.clients.values()


def liveLoader(PageClass, FactoryClass=LivePageFactory):
    """
    Helper for handling Page creation for LivePage applications.

    Example::

        class Foo(Page):
            child_app = liveLoader(MyLivePage)

    This is an experimental convenience function.  Consider it even less
    stable than the rest of this module.
    """
    fac = FactoryClass(PageClass)
    def liveChild(self, ctx):
        return fac.clientFactory(ctx)
    return liveChild


class LivePage(rend.Page):
    transportFactory = LivePageTransport
    transportLimit = 2
    _rendered = False

    factory = None
    _transportQueue = None
    _requestIDCounter = None
    _remoteCalls = None
    clientID = None

    _transportCount = 0
    _noTransportsDisconnectCall = None
    _didDisconnect = False

    TRANSPORTLESS_DISCONNECT_TIMEOUT = 30
    TRANSPORT_IDLE_TIMEOUT = 300

#     A note on timeout/disconnect logic: whenever a live client goes from some
#     transports to no transports, a timer starts; whenever it goes from no
#     transports to some transports, the timer is stopped; if the timer ever
#     expires the connection is considered lost; every time a transport is
#     added a timer is started; when the transport is used up, the timer is
#     stopped; if the timer ever expires, the transport has a no-op sent down
#     it; if an idle transport is ever disconnected, the connection is
#     considered lost; this lets the server notice clients who actively leave
#     (closed window, browser navigates away) and network congestion/errors
#     (unplugged ethernet cable, etc)

    def renderHTTP(self, ctx):
        assert not self._rendered, "Cannot render a LivePage more than once"
        assert self.factory is not None, "Cannot render a LivePage without a factory"
        self._rendered = True
        self._requestIDCounter = itertools.count().next
        self._transportQueue = defer.DeferredQueue(size=self.transportLimit)
        self._remoteCalls = {}
        self._disconnectNotifications = []
        self.clientID = self.factory.addClient(self)

        self._transportTimeouts = {}
        self._noTransports()

        neverEverCache(inevow.IRequest(ctx))
        return rend.Page.renderHTTP(self, ctx)

    def _noTransports(self):
        assert self._noTransportsDisconnectCall is None
        self._noTransportsDisconnectCall = reactor.callLater(
            self.TRANSPORTLESS_DISCONNECT_TIMEOUT, self._noTransportsDisconnect)

    def _someTransports(self):
        self._noTransportsDisconnectCall.cancel()
        self._noTransportsDisconnectCall = None

    def _newTransport(self, req):
        self._transportTimeouts[req] = reactor.callLater(
            self.TRANSPORT_IDLE_TIMEOUT, self._idleTransportDisconnect, req)

    def _noTransportsDisconnect(self):
        self._noTransportsDisconnectCall = None
        self._disconnected(error.TimeoutError("No transports created by client"))

    def _disconnected(self, reason):
        if not self._didDisconnect:
            self._didDisconnect = True
            notifications = self._disconnectNotifications
            self._disconnectNotifications = None
            for d in notifications:
                d.errback(reason)
            calls = self._remoteCalls
            self._remoteCalls = {}
            for (reqID, resD) in calls.iteritems():
                resD.errback(reason)
            self.factory.removeClient(self.clientID)

    def _idleTransportDisconnect(self, req):
        del self._transportTimeouts[req]
        # This is lame.  Queue may be the wrong way to store requests. :/
        def cbTransport((gotD, gotReq)):
            assert req is gotReq
            # We aren't actually sending a no-op here, just closing the
            # connection.  That's probably okay though.  The client will just
            # reconnect.
            gotD.callback([])
        self.getTransport().addCallback(cbTransport)

    def _activeTransportDisconnect(self, error, req):
        # XXX I don't think this will ever be a KeyError... but what if someone
        # wrote a response to the request, and halfway through the socket
        # kerploded... we might get here in that case?
        timeoutCall = self._transportTimeouts.pop(req, None)
        if timeoutCall is not None:
            timeoutCall.cancel()
        self._disconnected(error)

    def _ebOutput(self, err):
        msg = u"%s: %s" % (err.type.__name__, err.getErrorMessage())
        return 'throw new Error(%s);' % (json.serialize(msg),)

    def addTransport(self, req):
        neverEverCache(req)
        activeChannel(req)

        req.notifyFinish().addErrback(self._activeTransportDisconnect, req)

        # _transportCount can be negative
        if self._transportCount == 0:
            self._someTransports()
        self._transportCount += 1

        self._newTransport(req)

        d = defer.Deferred()
        d.addCallbacks(json.serialize, self._ebOutput)
        self._transportQueue.put((d, req))
        return d

    def getTransport(self):
        self._transportCount -= 1
        if self._transportCount == 0:
            self._noTransports()
        def cbTransport((d, req)):
            timeoutCall = self._transportTimeouts.pop(req, None)
            if timeoutCall is not None:
                timeoutCall.cancel()
            return (d, req)
        return self._transportQueue.get().addCallback(cbTransport)

    def _cbCallRemote(self, (d, req), methodName, args):
        requestID = u's2c%i' % (self._requestIDCounter(),)
        objectID = 0
        d.callback((requestID, None, (objectID, methodName, tuple(args))))

        resultD = defer.Deferred()
        self._remoteCalls[requestID] = resultD
        return resultD

    def callRemote(self, methodName, *args):
        d = self.getTransport()
        d.addCallback(self._cbCallRemote, unicode(methodName, 'ascii'), args)
        return d

    def notifyOnDisconnect(self):
        d = defer.Deferred()
        self._disconnectNotifications.append(d)
        return d

    def render_liveglue(self, ctx):
        if True:
            mk = tags.script(type='text/javascript', src=url.here.child("mochikit.js"))
        else:
            mk = [
              tags.script(type='text/javascript', src=url.here.child('MochiKit').child(fName))
              for fName in ['Base.js', 'Async.js']]

        return [
            tags.script(type='text/javascript')[tags.raw("""
                var nevow_livepageId = '%s';
            """ % self.clientID)],
            mk,
            tags.script(type='text/javascript', src=url.here.child('MochiKitLogConsole.js')),
            tags.script(type='text/javascript', src=url.here.child("athena.js")),
        ]

    _javascript = {'mochikit.js': 'MochiKit.js',
                   'athena.js': 'athena.js',
                   'MochiKitLogConsole.js': 'MochiKitLogConsole.js'}
    def childFactory(self, ctx, name):
        if name in self._javascript:
            return static.File(util.resource_filename('nevow', self._javascript[name]))

    def child_MochiKit(self, ctx):
        return static.File(util.resource_filename('nevow', 'MochiKit'))

    def child_MochiKitLogConsole(self, ctx):
        return static.File(util.resource_filename('nevow', 'MochiKit'))

    def child_transport(self, ctx):
        if self._rendered:
            return self.transportFactory(self)
        return rend.FourOhFour()

    def locateMethod(self, ctx, methodName):
        return getattr(self, 'remote_' + methodName)

    def remote_live(self, ctx):
        """
        Framework method invoked by the client when it is first
        loaded.  This simply dispatches to goingLive().
        """
        self.goingLive(ctx)
        self._onDisconnect = defer.Deferred()
        return self._onDisconnect

    def goingLive(self, ctx):
        pass