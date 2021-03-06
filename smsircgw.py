import codecs
import ConfigParser as configparser
import json
import mimetypes
import phonenumbers
import random
import requests
import sys
import tempfile
import time
import tinys3
from twilio.rest import TwilioRestClient
from twilio.rest.exceptions import TwilioRestException
import twilio.twiml
from twilio.util import RequestValidator
from twisted.web import server, resource
from twisted.words.protocols import irc
from twisted.internet import protocol, reactor, ssl

HELP_TEXT = '''Hi! I'm your friendly IRC-to-SMS gateway.

The commands you can run:
    REGISTER [username] [number] - register a username and a phone number
    VALIDATE [username] [code] - validate your phone number
    HELP - show this text

You can run these commands from your phone:
    !QUIET or STOP - stop receiving messages temporarily
    !HI - start receiving messages again
    !DEREGISTER - deregister your username and phone number
    !HELP or HELP - get this list of commands to your phone'''
SMS_HELP_TEXT = 'Commands: !QUIET/STOP, !HI, !DEREGISTER, !HELP/HELP'


class UserDatabase(object):

    def __init__(self, config, twilioclient):
        self.config = config
        self.filename = self.config['database_file']
        self.twilio = twilioclient
        self.database = {}
        self.read_database()

    def read_database(self):
        with codecs.open(self.filename, 'r', 'utf-8') as f:
            try:
                self.database = json.load(f, encoding='utf-8')
            except ValueError:
                self.database = {}
                self.write_database()

    def write_database(self):
        with codecs.open(self.filename, 'w', 'utf-8') as f:
            json.dump(self.database, f, encoding='utf-8', indent=1)

    def register_user(self, username, number):
        username = username.strip().lower()
        number = self.convert_to_e164(number)
        if not username or not number:
            raise ValueError('username or number is missing')
        if username in self.database.keys():
            raise ValueError('username already exists')
        if self.get_username(number):
            raise ValueError('username with that number already exists')
        auth_code = self.create_auth_code()
        self.database[username] = {'number': number,
                                   'auth_code': auth_code,
                                   'quiet': False}
        self.write_database()
        self.twilio.sms.messages.create(
            to=number, from_=self.config['phone_number'],
            body=('Hi, {0}! This is the IRC gateway. Your validation code '
                  'is: {1}'.format(username, auth_code)))

    def validate_user(self, username, auth_code):
        username = username.strip().lower()
        auth_code = auth_code.strip()
        if username in self.database.keys() and \
           self.database[username]['auth_code'] == auth_code:
            self.database[username]['auth_code'] = None
            self.write_database()
            return True
        return False

    def deregister_user(self, username):
        del self.database[username]
        self.write_database()

    def get_username(self, number):
        number = self.convert_to_e164(number)
        for key, value in self.database.items():
            if value['auth_code']:
                continue
            if value['number'] == number:
                return key

    def get_number(self, username):
        username = username.strip().lower()
        if username in self.database.keys() and \
           self.database[username]['auth_code'] is None:
            return self.database[username]['number']

    def set_quiet(self, username, quiet):
        username = username.strip().lower()
        if isinstance(quiet, bool):
            if username in self.database.keys():
                self.database[username]['quiet'] = quiet
                self.write_database()

    def get_quiet(self, username):
        username = username.strip().lower()
        if username in self.database.keys():
            return self.database[username]['quiet']

    @staticmethod
    def convert_to_e164(raw_phone):
        if not raw_phone:
            return

        if raw_phone[0] == '+':
            parse_type = None
        else:
            parse_type = 'US'

        phone_repr = phonenumbers.parse(raw_phone, parse_type)
        return phonenumbers.format_number(phone_repr,
                                          phonenumbers.PhoneNumberFormat.E164)

    @staticmethod
    def create_auth_code():
        return ''.join(map(str, random.sample(range(10), 6)))


class GatewayBot(irc.IRCClient):

    @property
    def nickname(self):
        return self.factory.config['irc_nick']

    # twisted overrides below

    def signedOn(self):
        if self.factory.login_message:
            self.msg('Userserv', self.factory.login_message)
        self.join(self.factory.channel)
        self.factory.smshandler.bot = self

    def connectionLost(self, reason):
        self.factory.smshandler.bot = None

    def privmsg(self, user, channel, msg):
        user = user.split('!', 1)[0]

        # private message
        if channel == self.nickname:
            split = msg.strip().split(' ', 1)
            command = split[0].lower()
            if len(split) > 1:
                args = split[1].split(' ')
            else:
                args = []
            if command == 'register':
                if len(args) != 2:
                    self.notice(user, 'Invalid number of arguments; 2 expected')
                    return
                try:
                    self.factory.database.register_user(args[0], args[1])
                    self.notice(user, ('I sent a validation code to your '
                                       'phone. Use the VALIDATE command to '
                                       'validate your phone.'))
                    return
                except ValueError, e:
                    self.notice(user, e.message)
                    return
                except phonenumbers.phonenumberutil.NumberParseException, e:
                    self.notice(user, e.message)
                    return
                except:
                    self.notice(user, 'Unexpected error')
                    raise
            elif command == 'validate':
                if len(args) != 2:
                    self.notice(user, 'Invalid number of arguments; 1 expected')
                    return
                if self.factory.database.validate_user(args[0], args[1]):
                    self.notice(user, 'Validation successful!')
                    return
                else:
                    self.notice(user, 'Validation failed.')
                    return
            elif command == 'help':
                for line in HELP_TEXT.splitlines():
                    self.notice(user, line)
                return
            else:
                self.notice(user, 'Unrecognized command. See HELP.')
                return

        # in-channel message
        msgsplit = msg.split(' ', 2)
        if msgsplit[0] in ('!msg', '!sms'):
            if len(msgsplit) > 1:
                number = self.factory.database.get_number(msgsplit[1])
                if number:
                    if self.factory.database.get_quiet(msgsplit[1]):
                        self.msg(channel, ('{0}: {1} has asked me to be '
                                           'quiet'.format(user, msgsplit[1])))
                    elif len(msgsplit) > 2 and msgsplit[2].strip():
                        body = '<{0}> {1}'.format(user, msgsplit[2].strip())
                        twilioc = self.factory.twilio
                        twilioc.sms.messages.create(
                            from_=self.factory.config['phone_number'],
                            to=number, body=body)
                        self.msg(channel, ('{0}: sent!'.format(user)))
                    else:
                        self.msg(channel, ('{0}: What should I tell '
                                           '{1}?'.format(user, msgsplit[1])))
                else:
                    self.msg(channel, ('{0}: I don\'t know who {1} '
                                       'is'.format(user, msgsplit[1])))
        elif msgsplit[0].startswith(self.nickname):
            self.msg(channel, ('{0}: I respond to !msg or !sms'.format(user)))

    def kickedFrom(self, channel, kicker, message):
        self.join(channel)

    # my methods

    def sms_recv(self, number, body, medias):
        username = self.factory.database.get_username(number)
        if username:
            twilioc = self.factory.twilio
            msg = ['<{0}> {1}'.format(username, body).strip()]
            for media in medias:
                ext = mimetypes.guess_extension(media['mime'])
                if ext is None:
                    ext = ''
                if ext == '.jpe':  # wtf python
                    ext = '.jpg'
                filename = '{0}{1}{2}'.format(
                    self.factory.config['s3_bucket_prefix'],
                    int(time.time() * 1e6), ext)
                s3 = tinys3.Connection(
                    self.factory.config['s3_access_key'],
                    self.factory.config['s3_secret_key'],
                    endpoint=self.factory.config['s3_endpoint'], tls=True)
                try:
                    req = requests.get(media['url'], stream=True)
                except requests.exceptions.HTTPError:
                    msg.append('\x034[media download failed]\x03')
                    continue
                f = tempfile.TemporaryFile()
                for chunk in req.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
                f.flush()
                f.seek(0)
                try:
                    s3.upload(filename, f, self.factory.config['s3_bucket'])
                except requests.exceptions.HTTPError:
                    msg.append('\x034[media upload failed]\x03')
                    continue
                msg.append('\x0311http://{0}/{1}\x03'.format(
                    self.factory.config['s3_bucket'], filename))
            self.msg(self.factory.channel, ' '.join(msg))
            # delete the media from twilio
            for media in medias:
                while True:
                    try:
                        message_sid, unused, media_sid = \
                            media['url'].split('/')[-3:]
                        twilioc.media(message_sid).delete(media_sid)
                    except TwilioRestException as exc:
                        if exc.status == 400:
                            time.sleep(5)
                            continue
                        else:
                            if exc.code != 20404:
                                print(exc.msg)
                            break


class GatewayBotFactory(protocol.ClientFactory):

    def __init__(self, config, smshandler):
        self.config = config
        self.channel = self.config['irc_channel']
        self.smshandler = smshandler
        self.login_message = self.config.get('login_message', None)

        self.twilio = TwilioRestClient(self.config['twilio_account_sid'],
                                       self.config['twilio_auth_token'])
        self.database = UserDatabase(self.config, self.twilio)

    def buildProtocol(self, addr):
        p = GatewayBot()
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        reactor.stop()


class IndexPage(resource.Resource):

    def render_GET(self, request):
        return ('<body style="background-color:#fff">'
                '<pre style="color:#eee">There\'s nothing to see here</pre>'
                '</body>')


class SMSHandlerPage(resource.Resource):
    isLeaf = True
    bot = None

    def __init__(self, config):
        resource.Resource.__init__(self)
        self.config = config

    def render_GET(self, request):
        return self.render_POST(request)

    def render_POST(self, request):
        if 'From' not in request.args or 'Body' not in request.args or \
                not request.requestHeaders.hasHeader('X-Twilio-Signature'):
            request.setResponseCode(400)
            return ''
        request.setHeader('content-type', 'text/xml')
        resp = twilio.twiml.Response()
        args = dict((k, v[0]) for k, v in request.args.items())
        validator = RequestValidator(self.config['twilio_auth_token'])
        sig = request.requestHeaders.getRawHeaders('X-Twilio-Signature')[0]
        if not validator.validate(self.config['twilio_callback_url'], args,
                                  sig):
            print("Invalid Twilio signature")
            return str(resp)
        if self.bot:
            username = self.bot.factory.database.get_username(args['From'])
            if username:
                command = args['Body'].strip().lower()
                if command in ('!quiet', 'stop'):
                    self.bot.factory.database.set_quiet(username, True)
                    resp.sms('I won\'t send any messages to you. Send !HI '
                             'to have me start sending messages again.')
                elif command == '!hi':
                    self.bot.factory.database.set_quiet(username, False)
                    resp.sms('I\'ll be sending messages from IRC to you. '
                             'Send !QUIET to have me stop.')
                elif command == '!deregister':
                    self.bot.factory.database.deregister_user(username)
                    resp.sms('I\'ve forgotten who you are.')
                elif command in ('!help', 'help'):
                    resp.sms(SMS_HELP_TEXT)
                else:
                    request.write(str(resp))
                    request.finish()
                    media = []
                    for i in xrange(int(args['NumMedia'])):
                        istr = str(i)
                        media.append({
                            'url': args['MediaUrl'+istr],
                            'mime': args['MediaContentType'+istr],
                        })
                    self.bot.sms_recv(args['From'], args['Body'], media)
                    return 1
        return str(resp)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.stderr.write('usage: smsircgw.py CONFIG\n')
        sys.exit(1)

    # Configuration
    parser = configparser.SafeConfigParser()
    with open(sys.argv[1]) as f:
        parser.readfp(f)
    config = dict(parser.items('smsircgw'))
    config['irc_port'] = int(config['irc_port'])
    config['http_server_port'] = int(config['http_server_port'])

    # HTTP
    root = resource.Resource()
    root.putChild('', IndexPage())
    smshandler = SMSHandlerPage(config)
    root.putChild('onsms', smshandler)
    reactor.listenTCP(config['http_server_port'], server.Site(root))

    # IRC
    f = GatewayBotFactory(config, smshandler)
    reactor.connectSSL(config['irc_host'], config['irc_port'], f,
                       ssl.ClientContextFactory())

    reactor.run()
