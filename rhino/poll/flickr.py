from hashlib import md5
import json
from urllib import urlencode
from urlparse import urlunparse

from django.conf import settings
import httplib2

from rhino.models import Account, Media, Person


def account_for_flickr_id(nsid, person=None):
    try:
        return Account.objects.get(service='flickr.com', ident=nsid)
    except Account.DoesNotExist:
        pass

    result = call_flickr('flickr.people.getInfo', user_id=nsid)
    persondata = result['person']

    if person is None:
        if int(persondata['iconfarm']) == 0:
            avatar = None
        else:
            avatar = Media(
                image_url='http://farm%s.static.flickr.com/%s/buddyicons/%s.jpg'
                    % (persondata['iconfarm'], persondata['iconserver'], nsid),
                width=48,
                height=48,
            )
            avatar.save()
        person = Person(
            display_name=persondata['realname']['_content'],
            permalink_url=persondata['profileurl']['_content'],
            avatar=avatar,
        )
        person.save()

    try:
        display_name = persondata['realname']['_content']
    except KeyError:
        display_name = None

    acc = Account(
        service='flickr.com',
        ident=nsid,
        display_name=display_name or nsid,
        person=person,
    )
    acc.save()
    return acc


def object_for_flickr_object(flobj):
    pass


def object_from_url(url):
    pass


def sign_flickr_query(query):
    sign_base = ''.join('%s%s' % (k, v) for k, v in sorted(query.items(), key=lambda i: i[0]))

    signer = md5()
    signer.update(settings.FLICKR_KEY[1])
    signer.update(sign_base)

    query['api_sig'] = signer.hexdigest()


def call_flickr(method, sign=False, **kwargs):
    query = dict(kwargs)
    query['api_key'] = settings.FLICKR_KEY[0]
    query['method'] = method
    query['format'] = 'json'
    query['nojsoncallback'] = 1

    if sign:
        sign_flickr_query(query)

    url = urlunparse(('http', 'api.flickr.com', 'services/rest/', None, urlencode(query), None))

    h = httplib2.Http()
    resp, cont = h.request(url)
    if resp.status != 200:
        raise ValueError("Unexpected response querying Flickr method %s: %d %s" % (method, resp.status, resp.reason))

    result = json.loads(cont)
    if result.get('stat') != 'ok':
        raise ValueError("Error making Flickr request with method %s: %s" % (method, result.get('message')))

    return result


def photo_url_for_photo(photodata):
    return 'http://farm%(farm)s.static.flickr.com/%(server)s/%(id)s_%(secret)s_b.jpg' % photodata


def poll_flickr(account):
    user = account.person.user
    if user is None:
        return

    recent = call_flickr('flickr.photos.getContactsPhotos', sign=True, auth_token=account.authinfo, extras='date_upload')
    for photodata in recent['photos']:
        try:
            obj = Object.objects.get(service='flickr.com', foreign_id=photodata['id'])
        except Object.DoesNotExist:
            obj = Object(
                service='flickr.com',
                foreign_id=photodata['id'],
                render_mode='photo',
                title=photodata['title'],
                #body=,
                #time=,
                permalink_url='http://www.flickr.com/photos/%(owner)s/%(id)s/' % photodata,
                author=account_for_flickr_id(photodata['owner']),
            )
            obj.save()

        UserStream.objects.get_or_create(user=user, obj=obj,
            defaults={'time': obj.time, 'why_account': obj.author, 'why_verb': 'post'})