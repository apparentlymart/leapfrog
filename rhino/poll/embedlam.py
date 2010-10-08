from HTMLParser import HTMLParseError
import logging
import urllib2

from BeautifulSoup import BeautifulSoup
import oembed


log = logging.getLogger(__name__)


class DiscoveryConsumer(oembed.OEmbedConsumer):

    def _endpointFor(self, url):
        endpoint = super(DiscoveryConsumer, self)._endpointFor(url)
        if endpoint is None:
            endpoint = self.discoverEndpoint(url)
        return endpoint

    def discoverEndpoint(self, url):
        opener = urllib2.build_opener()
        opener.addheaders = (
            ('User-Agent', 'python-oembed/' + oembed.__version__),
        )

        response = opener.open(url)

        headers = response.info()
        try:
            content_type = headers['Content-Type']
        except KeyError:
            raise oembed.OEmbedError('Resource targeted for discovery has no Content Type')
        if not 'html' in content_type.lower():
            raise oembed.OEmbedError('Resource targeted for discovery is %s, not an HTML page' % content_type)

        try:
            page = BeautifulSoup(response.read())
        except HTMLParseError:
            raise oembed.OEmbedError('Could not discover against invalid HTML target %s' % url)
        head = page.head
        if head is None:
            raise oembed.OEmbedError('Could not discover against HTML target %s with no head' % url)

        oembed_node = head.find(rel='alternate', type='application/json+oembed')
        if oembed_node is None:
            oembed_node = head.find(rel='alternate', type='text/xml+oembed')
        if oembed_node is None:
            raise oembed.OEmbedError('Could not discover against HTML target %s with no oembed tags' % url)

        return oembed.OEmbedEndpoint(oembed_node['href'])


class EmbedError(Exception):
    pass


def embed(url):
    csr = DiscoveryConsumer()
    endp = oembed.OEmbedEndpoint('http://www.flickr.com/services/oembed',
        ['http://*.flickr.com/*', 'http://flic.kr/*'])
    csr.addEndpoint(endp)

    try:
        resource = csr.embed(url)
    except (oembed.OEmbedError, urllib2.HTTPError), exc:
        raise EmbedError('%s trying to embed %s: %s' % (type(exc).__name__, url, str(exc)))

    # well
    log.debug('YAY OEMBED ABOUT %s: %r', url, resource.getData())

    return resource
