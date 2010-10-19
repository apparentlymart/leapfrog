from datetime import datetime
import json
import logging
import re

from django.conf import settings
import httplib2
import oauth2 as oauth

from rhino.models import Object, Account, Person, UserStream, Media, UserReplyStream
import rhino.poll.embedlam


log = logging.getLogger(__name__)


def account_for_twitter_user(userdata, person=None):
    try:
        account = Account.objects.get(service='twitter.com', ident=str(userdata['id']))
    except Account.DoesNotExist:
        if person is None:
            avatar = Media(
                width=48,
                height=48,
                image_url=userdata['profile_image_url'],
            )
            avatar.save()
            person = Person(
                display_name=userdata['name'],
                avatar=avatar,
                permalink_url='http://twitter.com/%s' % userdata['screen_name'],
            )
            person.save()
        account = Account(
            service='twitter.com',
            ident=str(userdata['id']),
            display_name=userdata['name'],
            person=person,

            status_background_color=userdata['profile_background_color'],
            status_background_image_url=userdata['profile_background_image_url'],
            status_background_tile=userdata['profile_background_tile'],
        )
        account.save()

    return account


def tweet_html(tweetdata):
    tweet = tweetdata['text']
    if 'entities' not in tweetdata:
        return tweet

    mutations = list()
    for urldata in tweetdata['entities'].get('urls', ()):
        url = urldata['expanded_url'] or urldata['url']
        mutations.append({
            'indices': urldata['indices'],
            'html': """<a href="%s">%s</a>""" % (url, url),
        })
    for mentiondata in tweetdata['entities'].get('user_mentions', ()):
        mutations.append({
            'indices': mentiondata['indices'],
            'html': """@<a href="http://twitter.com/%(screen_name)s" title="%(name)s">%(screen_name)s</a>""" % mentiondata,
        })
    for tagdata in tweetdata['entities'].get('hashtags', ()):
        mutations.append({
            'indices': tagdata['indices'],
            'html': """<a href="http://twitter.com/search?q=%%23%(text)s">#%(text)s</a>""" % tagdata,
        })

    for mutation in sorted(mutations, key=lambda x: x['indices'][0], reverse=True):
        indices = mutation['indices']
        tweet = tweet[:indices[0]] + mutation['html'] + tweet[indices[1]:]

    return tweet


url_re = re.compile(r"""
    (?: (?<= \A )
      | (?<= [\s.:;?\-\]<\(] ) )

    https?://
    [-\w;/?:@&=+$.!~*'()%,#]+
    [\w/]

    (?= \Z | [\s\.,!:;?\-\[\]>\)] )
""", re.IGNORECASE | re.MULTILINE | re.DOTALL | re.VERBOSE)

mention_re = re.compile(r"""
    (?: (?<= \A ) | (?<= \s ) )
    @ (\w+)
""", re.IGNORECASE | re.MULTILINE | re.DOTALL | re.VERBOSE)

tag_re = re.compile(r"""
    (?: (?<= \A ) | (?<= \s ) )  # BOT or whitespace
    \# (\w\S*\w)      # tag
    (?<! 's )         # but don't end with 's
""", re.IGNORECASE | re.MULTILINE | re.DOTALL | re.VERBOSE)

def synthesize_entities(tweetdata):
    if 'entities' in tweetdata:
        return

    tweettext = tweetdata['text']
    ents = {
        'urls': [],
        'hashtags': [],
        'user_mentions': [],
    }

    for match in url_re.finditer(tweettext):
        ents['urls'].append({
            'expanded_url': None,
            'url': match.group(0),
            'indices': [match.start(), match.end()],
        })

    for match in mention_re.finditer(tweettext):
        ents['user_mentions'].append({
            'id': None,  # we would have to get this if we ever used it
            'name': match.group(1),  # TODO: get this from the API? ugh
            'screen_name': match.group(1),
            'indices': [match.start(), match.end()],
        })

    for match in tag_re.finditer(tweettext):
        ents['hashtags'].append({
            'text': match.group(1),
            'indices': [match.start(), match.end()],
        })

    tweetdata['entities'] = ents


def object_from_twitpic_url(url):
    mo = re.match(r'http://twitpic\.com/(\w+)', url)
    twitpic_id = mo.group(1)

    try:
        return Object.objects.get(service='twitpic.com', foreign_id=twitpic_id)
    except Object.DoesNotExist:
        pass

    h = httplib2.Http()
    resp, content = h.request('http://api.twitpic.com/2/media/show.json?id=%s' % twitpic_id)

    picdata = json.loads(content)
    userdata = picdata['user']
    # ugh, why did they rename these
    userdata['id'] = userdata['twitter_id']
    userdata['screen_name'] = userdata['username']
    userdata['profile_image_url'] = userdata['avatar_url']

    pic = Media(
        image_url='http://twitpic.com/show/large/%s' % twitpic_id,
        width=int(picdata['width']),
        height=int(picdata['height']),
    )
    pic.save()
    obj = Object(
        service='twitpic.com',
        foreign_id=twitpic_id,
        render_mode='image',
        title=picdata['message'],
        image=pic,
        author=account_for_twitter_user(userdata),
        time=datetime.strptime(picdata['timestamp'], '%Y-%m-%d %H:%M:%S'),
        permalink_url=url,
    )
    obj.save()
    return obj


def raw_object_for_tweet(tweetdata, client):
    """Returns the normalized Object for the given tweetdata, and whether
    that Object represents that tweet or the thing the tweet is sharing.

    This is returned as a tuple containing (a) whether the tweet is a
    share, and (b) the ``rhino.models.Object`` reference for that tweet
    data, in that order.

    """
    try:
        return False, Object.objects.get(service='twitter.com', foreign_id=str(tweetdata['id']))
    except Object.DoesNotExist:
        pass

    log.debug('Making new tweet for %s status #%d', tweetdata['user']['screen_name'], tweetdata['id'])

    in_reply_to = None
    if tweetdata.get('in_reply_to_status_id'):
        # Uh oh, gotta get that tweet.
        # TODO: remove the mention from the front of the tweet
        next_tweetid = tweetdata['in_reply_to_status_id']
        try:
            in_reply_to = Object.objects.get(service='twitter.com', foreign_id=str(next_tweetid))
        except Object.DoesNotExist:
            resp, content = client.request('http://api.twitter.com/1/statuses/show/%d.json'
                % next_tweetid)
            if resp.status != 200:
                raise ValueError("Unexpected %d %s response fetching tweet #%s up a reply chain for "
                    "%s's timeline" % (resp.status, resp.reason, next_tweetid, account.display_name))

            next_tweetdata = json.loads(content)
            synthesize_entities(next_tweetdata)
            log.debug("    Let's make a new tweet for %s status #%d", next_tweetdata['user']['screen_name'], next_tweetdata['id'])
            # If it's really a share, this tweet is transitively in reply to the shared thing, so this is fine.
            really_a_share, in_reply_to = raw_object_for_tweet(next_tweetdata, client)

    # Is this something other than a status?
    elif len(tweetdata['entities']['urls']) == 1:
        about_urldata = tweetdata['entities']['urls'][0]
        about_url = about_urldata['expanded_url'] or about_urldata['url']

        try:
            in_reply_to = rhino.poll.embedlam.object_for_url(about_url)
        except ValueError, exc:
            log.debug(str(exc))
        else:
            # If the tweet was only the object's url and its title, make it a share.
            tweettext = tweetdata['text']
            link_starts, link_ends = about_urldata['indices']
            tweettext = tweettext[:link_starts] + tweettext[link_ends:]
            tweettext = tweettext.lower()
            if in_reply_to.title:
                tweettext = tweettext.replace(in_reply_to.title.lower(), '')
            tweettext = re.sub(r'\s', '', tweettext)

            if not tweettext:
                return True, in_reply_to

    tweet = Object(
        service='twitter.com',
        foreign_id=str(tweetdata['id']),
        render_mode='status',
        body=tweet_html(tweetdata),
        time=datetime.strptime(tweetdata['created_at'], '%a %b %d %H:%M:%S +0000 %Y'),
        permalink_url='http://twitter.com/%s/status/%d'
            % (tweetdata['user']['screen_name'], tweetdata['id']),
        author=account_for_twitter_user(tweetdata['user']),
        in_reply_to=in_reply_to,
    )
    tweet.save()

    return False, tweet


def poll_twitter(account):
    user = account.person.user
    if user is None:
        return

    # Get that twitter user's home timeline.
    csr = oauth.Consumer(*settings.TWITTER_CONSUMER)
    token = oauth.Token(*account.authinfo.split(':', 1))
    client = oauth.Client(csr, token)
    resp, content = client.request('http://api.twitter.com/1/statuses/home_timeline.json?include_entities=true', 'GET')
    if resp.status != 200:
        raise ValueError("Unexpected %d %s response fetching %s's twitter timeline"
            % (resp.status, resp.reason, account.display_name))

    tl = json.loads(content)

    for orig_tweetdata in reversed(tl):
        try:
            # TODO: filter based on source?

            tweetdata = orig_tweetdata
            why_verb = 'post'
            try:
                tweetdata = orig_tweetdata['retweeted_status']
            except KeyError:
                pass
            else:
                why_verb = 'share'

            orig_actor = account_for_twitter_user(orig_tweetdata['user'])
            really_a_share, tweet = raw_object_for_tweet(tweetdata, client)
            if really_a_share:
                why_verb = 'share'

            # CASES:
            # real reply to...
            # real retweet of...
            # tweet with just a link
            # tweet with a link and custom text
            # tweet with a link and the link's target page title (found how?)

            if why_verb == 'post' and not tweet.in_reply_to:
                UserStream.objects.get_or_create(user=user, obj=tweet,
                    defaults={'why_account': tweet.author, 'why_verb': 'post', 'time': tweet.time})

            # But if it's really a reply?
            elif why_verb == 'post':
                root = tweet
                while root.in_reply_to is not None:
                    log.debug('Walking up from %r to %r', root, root.in_reply_to)
                    root = root.in_reply_to

                UserStream.objects.get_or_create(user=user, obj=root,
                    defaults={'why_account': tweet.author, 'why_verb': 'reply', 'time': tweet.time})

                # Now add a reply for each tweet in the thread.
                supertweet = tweet
                while supertweet.in_reply_to is not None:
                    UserReplyStream.objects.get_or_create(user=user, root=root, reply=supertweet,
                        defaults={'root_time': root.time, 'reply_time': supertweet.time})
                    supertweet = supertweet.in_reply_to

            elif why_verb == 'share':
                # Sharing is transitive, so really share the root.
                root = tweet
                while root.in_reply_to is not None:
                    log.debug('Walking up from %r to %r', root, root.in_reply_to)
                    root = root.in_reply_to

                UserStream.objects.get_or_create(user=user, obj=root,
                    defaults={'why_account': orig_actor, 'why_verb': 'share', 'time': tweet.time})

        except Exception, exc:
            log.exception(exc)
