# allow imports from the top-level dir (we will want to make the package
# system better later)

from imapclient import IMAPClient

import sys
sys.path.insert(0, "..")

import datetime
import logging as log
import tornado

import encoding
from models import MessageMeta, MessagePart, FolderMeta
import time
import json

from quopri import decodestring as quopri_decodestring
from base64 import b64decode
from chardet import detect as detect_charset


IMAP_HOST = 'imap.gmail.com'
SMTP_HOST = 'smtp.gmail.com'


class AuthFailure(Exception): pass
class TooManyConnectionsFailure(Exception): pass



class CrispinClient:
    # 20 minutes
    SERVER_TIMEOUT = datetime.timedelta(seconds=1200)

    def __init__(self, user_obj):

        self.user_obj = user_obj
        self.email_address = user_obj.g_email
        self.oauth_token = user_obj.g_access_token
        self.imap_server = None
        # last time the server checked in, in UTC
        self.keepalive = None



    def print_duration(fn):
        """ A decorator for methods that can only be run on a logged-in client.
        """
        def connected_fn(self, *args, **kwargs):
            start_time = time.time()
            ret = fn(self, *args, **kwargs)
            log.info("\t\tTook %s seconds" %  str(time.time() - start_time))
            return ret
        return connected_fn


    def server_needs_refresh(self):
        """ Many IMAP servers have a default minimum "no activity" timeout
            of 30 minutes. Sending NOPs ALL the time is hells slow, but we
            need to do it at least every 30 minutes.
        """
        now = datetime.datetime.utcnow()
        return self.keepalive is None or \
                (now - self.keepalive) > self.SERVER_TIMEOUT


    def connected(fn):
        """ A decorator for methods that can only be run on a logged-in client.
        """
        def connected_fn(self, *args, **kwargs):
            if self.server_needs_refresh():
                self._connect()
            ret = fn(self, *args, **kwargs)
            # a connected function did *something* with our connection, so
            # update the keepalive
            self.keepalive = datetime.datetime.utcnow()
            return ret
        return connected_fn


    def _connect(self):
        log.info('Connecting to %s ...' % IMAP_HOST,)

        try:
            self.imap_server.noop()
            if self.imap_server.state == 'NONAUTH' or self.imap_server.state == 'LOGOUT':
                raise Exception
            log.info('Already connected to host.')
            return True
        # XXX eventually we want to do stricter exception-checking here
        except Exception, e:
            log.info('No active connection. Opening connection...')

        try:
            self.imap_server = IMAPClient(IMAP_HOST, use_uid=True,
                    ssl=True)
            # self.imap_server.debug = 4  # todo
            log.info("Logging in: %s" % self.email_address)
            self.imap_server.oauth2_login(self.email_address, self.oauth_token)

        except Exception as e:
            if str(e) == '[ALERT] Too many simultaneous connections. (Failure)':
                raise TooManyConnectionsFailure("Too many simultaneous connections.")
            elif str(e) == '[ALERT] Invalid credentials (Failure)':
                raise AuthFailure("Invalid credentials")
            else:
                raise e
                log.error(e)

            self.imap_server = None
            return False

        log.info('Connection successful.')
        return True


    def stop(self):
        log.info("Closing connection.")
        if (self.imap_server):
            self.imap_server.logout()


    @connected
    @print_duration
    def select_folder(self, folder):
        try:
            select_info = self.imap_server.select_folder(folder, readonly=True)
        except Exception, e:
            log.error(e)
            raise e
        log.info('Selected folder %s with %d messages.' % (folder, select_info['EXISTS']) )
        return select_info



    @connected
    @print_duration
    def fetch_folder(self, folder_name):
        log.info("Fetching messages in %s" % folder_name)

        select_info = self.select_folder(folder_name)
        # TODO check and save UID validity
        UIDs = self.imap_server.search(['NOT DELETED'])
        UIDs = [str(s) for s in UIDs]

        log.info("\n%i UIDs" % len(UIDs)  )

        return self.fetch_uids(UIDs)



    def fetch_uids(self, UIDs):
        """
        """

        query = 'BODY.PEEK[] ENVELOPE INTERNALDATE FLAGS'
        # log.info("Fetching message headers. Query: %s" % query)
        raw_messages = self.imap_server.fetch(UIDs,
                [query, 'X-GM-THRID', 'X-GM-MSGID', 'X-GM-LABELS'])
        # log.info("Found %i messages." % len(raw_messages))

        messages = []
        # NOTE: this processes messages in an unknown order
        for uid, msg in raw_messages.iteritems():
            # log.info("processing uid {0}".format(uid))
            messages.append((uid, msg['INTERNALDATE'], msg['FLAGS'],
                # python's email package (which lamson uses directly) needs
                # encoded bytestrings as its input, since to deal properly
                # with MIME-encoded email you need to do part decoding based on
                # message / MIME part headers anyway. imapclient tries to abstract
                # away bytes and decodes all bytes received from the wire as
                # _latin-1_, which is wrong in any case where 8bit MIME is
                # used. so we have to reverse the damage before we proceed.
                encoding.from_string(msg['BODY[]'].encode('latin-1')),
                msg['X-GM-THRID'], msg['X-GM-MSGID'], msg['X-GM-LABELS']))

        new_messages, new_parts, new_foldermeta = [], [], []
        for uid, internaldate, flags, mailbase, \
                x_gm_thrid, x_gm_msgid, x_gm_labels in messages:
            new_msg = MessageMeta()

            new_msg.g_user_id = self.user_obj.g_user_id
            new_msg.uid = unicode(uid)
            # XXX maybe eventually we want to use these, but for
            # backcompat for now let's keep in the
            # new_msg.subject = mailbase.headers.get('Subject')
            # new_msg.from_addr = mailbase.headers.get('From')
            # new_msg.sender_addr = mailbase.headers.get('Sender')
            # new_msg.reply_to = mailbase.headers.get('Reply-To')
            # new_msg.to_addr = mailbase.headers.get('To')
            # new_msg.cc_addr = mailbase.headers.get('Cc')
            # new_msg.bcc_addr = mailbase.headers.get('Bcc')
            # new_msg.in_reply_to = mailbase.headers.get('In-Reply-To')
            # new_msg.message_id = mailbase.headers.get('Message-Id')

            msg_envelope = msg['ENVELOPE'] # ENVELOPE is parsed RFC-2822 headers

            def make_unicode_contacts(contact_list):
                n = []
                for c in contact_list:
                    new_c = [None]*len(c)
                    for i in range(len(c)):
                        new_c[i] = encoding.make_unicode_header(c[i])
                    n.append(new_c)
                return n

            tempSubject = encoding.make_unicode_header(msg_envelope[1])
            # Headers will wrap when longer than 78 lines per RFC822_2
            tempSubject = tempSubject.replace('\n\t', '').replace('\r\n', '')
            new_msg.subject = tempSubject
            new_msg.from_addr = msg_envelope[2]
            new_msg.sender_addr = msg_envelope[3]
            new_msg.reply_to = msg_envelope[4]
            new_msg.to_addr = msg_envelope[5]
            new_msg.cc_addr = msg_envelope[6]
            new_msg.bcc_addr = msg_envelope[7]
            new_msg.in_reply_to = msg_envelope[8]
            new_msg.message_id = msg_envelope[9]

            new_msg.internaldate = internaldate
            new_msg.g_thrid = unicode(x_gm_thrid)
            new_msg.g_msgid = unicode(x_gm_msgid)


            def make_fm(label):
                fm = FolderMeta()
                fm.g_email = self.email_address
                fm.g_msgid = new_msg.g_msgid
                fm.folder_name = label
                fm.msg_uid = new_msg.uid
                return fm

            for label in x_gm_labels:
                new_foldermeta.append(make_fm(label))
            # Need to manually create one for All Mail
            fm = make_fm(self.all_mail_folder_name())
            new_foldermeta.append(fm)


            # TODO parse out flags and store as enum instead of string
            # \Seen  Message has been read
            # \Answered  Message has been answered
            # \Flagged  Message is "flagged" for urgent/special attention
            # \Deleted  Message is "deleted" for removal by later EXPUNGE
            # \Draft  Message has not completed composition (marked as a draft).
            # \Recent   session is the first session to have been notified about this message
            new_msg.flags = unicode(flags)

            new_messages.append(new_msg)


            # new_parts.append


            # TODO store these
            i = 0  # for walk_index

            # # Store all message headers as object with index 0
            headers_part = MessagePart()
            headers_part.g_msgid = new_msg.g_msgid
            headers_part.walk_index = i
            headers_part.data = json.dumps(mailbase.headers)

            new_parts.append(headers_part)



            for part in mailbase.walk():
                i += 1
                mimepart = encoding.to_message(part)
                if mimepart.is_multipart(): continue  # TODO should we store relations?

                new_part = MessagePart()
                new_part.g_msgid = new_msg.g_msgid
                new_part.walk_index = i

                new_part.misc_keyval = mimepart.items()  # everything

                # Content-Type
                try:
                    assert mimepart.get_content_type() == mimepart.get_params()[0][0],\
                    "Content-Types not equal!  %s and %s" (mimepart.get_content_type(), mimepart.get_params()[0][0])
                except Exception, e:
                    log.error("Content-Types not equal: %s" % mimepart.get_params())

                new_part.content_type = mimepart.get_content_type()


                # File attachments
                filename = mimepart.get_filename(failobj=None)
                if filename: encoding.make_unicode_header(filename)
                new_part.filename = filename

                # Make sure MIME-Version is 1.0
                mime_version = mimepart.get('MIME-Version', failobj=None)
                if mime_version and mime_version != '1.0':
                    log.error("Unexpected MIME-Version: %s" % mime_version)




                # Content-Disposition attachment; filename="floorplan.gif"
                content_disposition = mimepart.get('Content-Disposition', None)
                if content_disposition:
                    content_disposition = content_disposition.split(';')[0].lower()
                    assert content_disposition in ['inline', 'attachment'], "Unknown Content Disposition: %s" % content_disposition
                new_part.content_disposition = content_disposition
                new_part.content_id = mimepart.get('Content-Id', None)


                # DEBUG -- not sure if these are ever really used in emails
                if mimepart.preamble:
                    log.warning("Found a preamble! " + mimepart.preamble)
                if mimepart.epilogue:
                    log.warning("Found an epilogue! " + mimepart.epilogue)


                payload_data = mimepart.get_payload(decode=False)  # decode ourselves
                data_encoding = mimepart.get('Content-Transfer-Encoding', None).lower()

                if data_encoding == 'quoted-printable':
                    data_to_write = quopri_decodestring(payload_data)

                elif data_encoding == 'base64':
                    data_to_write = b64decode(payload_data)

                elif data_encoding == '7bit' or data_encoding == '8bit':
                    # Need to get charset and decode with that too.
                    charset = str(mimepart.get_charset())
                    if charset == 'None': charset = None  # bug
                    try:
                        assert charset
                        payload_data = encoding.attempt_decoding(charset, payload_data)
                    except Exception, e:
                        detected_charset = detect_charset(payload_data)
                        detected_charset_encoding = detected_charset['encoding']
                        log.error("%s Failed decoding with %s. Now trying %s" % (e, charset , detected_charset_encoding))
                        try:
                            payload_data = encoding.attempt_decoding(detected_charset_encoding, payload_data)
                        except Exception, e:
                            log.error("That failed too. Hmph. Not sure how to recover here")
                            raise e
                        log.info("Success!")
                    data_to_write = payload_data.encode('utf-8')
                else:
                    raise Exception("Unknown encoding scheme:" + str(encoding))


                new_part.data = data_to_write
                new_parts.append(new_part)



            # bodystructure = message_dict['BODY']

            # def create_messagepart(p, section='1'):
            #     assert len(p) > 0

            #     part = MessagePart()
            #     part.section = str(section)
            #     part.g_msgid = new_msg.g_msgid
            #     part.allmail_uid = str(message_uid)

            #     if len(p) == 1:
            #         if p[0].lower() == "appledouble":
            #             log.error("Content-type: appledouble")
            #             # print p
            #             return None
            #         else:
            #             log.error("Why is there only one content-type part? Should it be multipart/%s ??" % p[0])
            #             return part

            #     content_type_major = p[0].lower()
            #     content_type_minor = p[1].lower()

            #     part.content_type = "%s/%s" % (content_type_major, content_type_minor)

            #     if len(p) == 2: return part

            #     assert len(p) >= 7, p
            #     part.encoding = p[5]  # Content-Transfer-Encoding
            #     part.bytes = p[6]

            #     # Example for p[2] is ("CHARSET" "ISO-8859-1" "FORMAT" "flowed")  or  ("NAME" "voicemail.wav")
            #     if p[2] and not isinstance(p[2], basestring):
            #         assert len(p[2]) % 2 == 0  # key/value pairs
            #         m = {}
            #         for i in range(0 , len(p[2]) ,2):  # other optional arguments.
            #             key = p[2][i].lower()
            #             val = p[2][i+1]

            #             if key == 'charset':
            #                 part.charset = val
            #             elif key == 'name':
            #                 part.filename = val
            #             else:
            #                 m[key] = val
            #         part.misc_keyval = m
            #     if content_type_major == 'text':
            #         assert len(p) == 8
            #         part.line_count = p[7]
            #     return part


            # def make_obj(p, i=''):
            #     if not isinstance(p[0], basestring):

            #         # This part removes the mime relation
            #         # print 'p here...', p
            #         if isinstance(p[-1], basestring):
            #             mime_relation = p[-1]
            #             if (len(p) == 2):
            #                 if isinstance(p[0][0], basestring):  # single object
            #                     toIterate = p[:-1]
            #                 else:  # Nested list
            #                     toIterate = p[0]
            #             else:  # probably have multiple objects here
            #                 toIterate = p[:-1]
            #         else:
            #             # No relationship var here
            #             log.error("NO MIME RELATION HERE.....")
            #             toIterate = p


            #         for x, part in enumerate(toIterate):

            #             if isinstance(part, basestring):
            #                 log.error("Multiple-nested content type? %s" % part)
            #                 continue


            #             if len(i) > 0:
            #                 section = i+'.' + str(x+1)
            #             else:
            #                 section = str(x+1)

            #             # print 'calling make_obj', part

            #             ret = make_obj(part, section)  # call recursively and add to lists

            #             if not ret:
            #                 continue
            #             # Relations are alternative, mixed, signed, related
            #             new_parts.append(ret)
            #         return

            #     else:
            #         if len(i) > 0: index = i+'.1'  ## is this a lie? TODO
            #         else: index = '1'
            #         # print 'NOT BASESTRING', p, type(p)
            #         return create_messagepart(p, i)


            # if not bodystructure.is_multipart:
            #     part = create_messagepart(bodystructure)
            #     new_parts.append(part)
            # else:
            #     make_obj(bodystructure)

            # new_messages.append(new_msg)

        return new_messages, new_parts, new_foldermeta



    @connected
    def fetch_messages(self, folder_name):
        new_messages, new_parts = self.fetch_folder(folder_name)
        return new_messages




    @connected
    def fetch_msg_body(self, msg_uid, section_index, readonly=True):
        msg_uid = str(msg_uid)

        log.info("Fetching %s <%s>" % (msg_uid, section_index))

        query = query_key = 'BODY[%s]' % section_index
        if readonly:
            query = 'BODY.PEEK[%s]' % section_index

        query_key = 'BODY[%s]' % section_index
        response = self.imap_server.fetch(msg_uid,
                                    [query, 'X-GM-THRID', 'X-GM-MSGID'])

        try:
            response_dict =  response[int(msg_uid)]
        except KeyError, e:
            log.error('Response: %s' % response)
            return "Error fetching."

        body_data = response_dict[query_key]
        message_id = response_dict['X-GM-MSGID']
        thread_id = response_dict['X-GM-THRID']

        return body_data




    @connected
    def fetch_msg_headers(self, folder, msg_uid, readonly=True):

        if isinstance(msg_uid, basestring):
            msg_uid = [msg_uid]
        msg_uid = [str(s) for s in msg_uid]

        self.select_folder(folder)

        log.info("Fetching headers in %s -- %s" % (folder, msg_uid))

        query = query_key = 'BODY[HEADER]'
        if readonly:
            query = 'BODY.PEEK[HEADER]'
        query_key = 'BODY[HEADER]'
        response = self.imap_server.fetch(msg_uid,
                                    [query, 'X-GM-THRID', 'X-GM-MSGID'])

        return response


    @connected
    def fetch_entire_msg(self, folder, msg_uid, readonly=True):

        if isinstance(msg_uid, basestring):
            msg_uid = [msg_uid]
        msg_uid = [str(s) for s in msg_uid]

        query = query_key = 'BODY[]'
        if readonly:
            query = 'BODY.PEEK[]'
        query_key = 'BODY[]'
        response = self.imap_server.fetch(msg_uid,
                                    [query])

        return response



    @connected
    def all_mail_folder_name(self):
        """ Note: XLIST is deprecated, so we just use LIST

            This finds the Gmail "All Mail" folder name by using a flag.
            If the user's inbox is localized to a different language, it will return
            the proper localized string.

            An example response with some other flags:

            # * LIST (\HasNoChildren) "/" "INBOX"
            # * LIST (\Noselect \HasChildren) "/" "[Gmail]"
            # * LIST (\HasNoChildren \All) "/" "[Gmail]/All Mail"
            # * LIST (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
            # * LIST (\HasNoChildren \Important) "/" "[Gmail]/Important"
            # * LIST (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
            # * LIST (\HasNoChildren \Junk) "/" "[Gmail]/Spam"
            # * LIST (\HasNoChildren \Flagged) "/" "[Gmail]/Starred"
            # * LIST (\HasNoChildren \Trash) "/" "[Gmail]/Trash"
        """
        resp = self.imap_server.list_folders()
        folders =  [dict(flags = f[0], delimiter = f[1], name = f[2]) for f in resp]
        for f in folders:
            if u'\\All' in f['flags']:
                return f['name']
        raise Exception("Couldn't find All Mail folder")


    @connected
    def msgids_for_thrids(self, thread_ids):
        """ Batch fetch to get all X-GM-THRIDs for a group of UIDs """
        self.imap_server.select_folder(self.all_mail_folder_name())
        # The boolean IMAP queries use reverse polish notation for
        # the query parameters. imaplib automatically adds parenthesis
        criteria = 'X-GM-THRID %s' % str(thread_ids[0])
        if len(thread_ids) > 1:
            for t in thread_ids[1:]:
                criteria = 'OR ' + criteria + ' X-GM-THRID %s' % str(t)
        return self.imap_server.search(criteria)







# log.info("Expanded to %i messages for %i thread IDs." % (len(all_msg_uids), len(thread_ids)))

# all_msgs = self.fetch_headers_for_uids(self.all_mail_folder_name(), all_msg_uids)

# return all_msgs

# threads = {}
# # Group by thread id
# for m in all_msgs:
#     if m.thread_id not in threads.keys():
#         new_thread = IBThread()
#         new_thread.thread_id = m.thread_id
#         threads[m.thread_id] = new_thread
#     t = threads[m.thread_id]
#     t.message_ids.append(m.message_id)
# all_threads = threads.values()

# log.info("Returning %i threads with total of %i messages." % (len(all_threads), len(all_msgs)))

# return all_threads
