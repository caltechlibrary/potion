'''
tind.py: code for interacting with a TIND server

This downloads data in the MARC XML format instead of other formats, because
it contains data not present in the other output formats.  In addition,
TIND doesn't seem to do any additional cleanup on the data in the other
formats, so there's no real advantage. E.g., I find title values in the other
formats ending with "/", some fields have trailing commas in their values, etc.

Authors
-------

Michael Hucka <mhucka@caltech.edu> -- Caltech Library

Copyright
---------

Copyright (c) 2021 by the California Institute of Technology.  This code
is open-source software released under a 3-clause BSD license.  Please see the
file "LICENSE" for more information.
'''

from   collections import namedtuple
from   commonpy.interrupt import wait
from   commonpy.network_utils import net
from   commonpy.exceptions import NoContent, ServiceFailure, RateLimitExceeded
import json
from   json import JSONDecodeError
from   lxml import etree

if __debug__:
    from sidetrack import log

from .exceptions import *
from .item import TindItem
from .thumbnail_utils import thumbnail_from_google, thumbnail_from_amazon
from .record import TindRecord


# Constants.
# .............................................................................

# Field names for finding different elements in a MARC XML record.
ELEM_RECORD       = '{http://www.loc.gov/MARC21/slim}record'
ELEM_CONTROLFIELD = '{http://www.loc.gov/MARC21/slim}controlfield'
ELEM_DATAFIELD    = '{http://www.loc.gov/MARC21/slim}datafield'
ELEM_SUBFIELD     = '{http://www.loc.gov/MARC21/slim}subfield'

# URL templates for retrieving a record from a TIND server.
# The first placeholder is for the host URL; the second is for an identifier.
# Use Python .format() to substitute the relevant values into the string.
_XML_USING_BARCODE = '{}/search?p=barcode%3A+{}&of=xm'
_XML_USING_TIND_ID = '{}/search?recid={}&of=xm'

# URL template for item data from a TIND server.
# The first placeholder is for the host URL; the second is for a TIND record id.
# Use Python .format() to substitute the relevant values into the string.
_JSON_USING_TIND_ID = '{}/nanna/bibcirc/{}/details'

# URL template for thumbnail images. The result is returned as JSON.
# The first placeholder is for the host URL; the second is for a TIND record id.
# Use Python .format() to substitute the relevant values into the string.
_THUMBNAIL_FOR_TIND_ID = '{}/nanna/thumbnail/{}'

# Time in seconds we pause if we hit the rate limit, and number of times we
# repeatedly wait before we give up entirely.
_RATE_LIMIT_SLEEP = 15
_MAX_SLEEP_CYCLES = 8


# Class definitions.
# .............................................................................

class Tind():
    '''Interface to a TIND.io server.'''

    def __init__(self, server_url):
        self.server_url = server_url


    def record(self, tind_id = None, marc_xml = None):
        '''Create a TindRecord object given either a TIND id or MARC XML.

        Keyword arguments "tind_id" and "marc_xml" are mutually exclusive.

        If "tind_id" is given, this will search the TIND server using the id,
        download the MARC XML returned by TIND, create a TindRecord object
        based on the data, then separately, also use TIND's TindItem record
        API to find all the items/holdings for the record, create
        corresponding TindItem objects for each, and add them to the "items"
        list within the TindRecord object. If the TIND server does not return
        a result for the id, this method raises a NotFound exception.

        If "marc_xml" is given, the XML must have been obtained using the
        MARC XML export feature of TIND.  This method will skip the
        preliminary TIND search and instead, create the record using the
        given XML string, then perform the same steps as described above for
        the case of tind_id.

        If neither "tind_id" nor "marc_xml" is given, this method returns an
        empty TindRecord object.
        '''
        if tind_id and marc_xml:
            raise ValueError(f'"tind_id" and "marc_xml" are mutually exclusive.')

        if tind_id:
            tind_id = str(tind_id)
            if not tind_id.isdigit():
                raise ValueError(f'Invalid argument: {tind_id} is not a number.')
            record = self._record_from_server(_XML_USING_TIND_ID, tind_id)
        elif marc_xml:
            if not marc_xml.startswith(b'<?xml'):
                raise ValueError(f'marc_xml argument does not appear to be XML.')
            record = self._record_from_xml(marc_xml)
        else:
            return TindRecord()

        if record:
            record.items = self._items_for_tind_id(tind_id or record.tind_id)
            for item in record.items:
                item.parent = record
            return record
        else:
            arg = tind_id if tind_id else 'given XML data'
            raise NotFound(f'No record found for {arg} in {self.server_url}')


    def item(self, barcode = None):
        '''Create a TindItem object given a barcode value.

        This will contact the TIND server and perform a search using the
        barcode value, then using the data returned, create a TindRecord
        object with an TindItem object within it, and finally return the
        TindItem object.  (TindItem objects contain a property referencing
        their parent TindRecord objects, so callers can get the TindRecord
        given the TindItem object.)

        If the TIND server does not return a result for the barcode, this
        method raises a NotFound exception.

        If no barcode is given, this returns an empty TindItem object.
        '''
        if not barcode:
            return TindItem()
        barcode = str(barcode)
        if not barcode.isdigit():
            raise ValueError(f'Invalid argument: {barcode} is not a number.')
        record = self._record_from_server(_XML_USING_BARCODE, barcode)
        if record:
            record.items = self._items_for_tind_id(record.tind_id)
            for item in record.items:
                item.parent = record
            for item in record.items:
                if item.barcode == barcode:
                    return item
            raise PotionError('TindRecord-TindItem mismatch -- please report this.')
        else:
            raise NotFound(f'No record found for {barcode} in {self.server_url}')


    def _record_from_server(self, search_template, id, retry = 0):
        (resp, error) = net('get', search_template.format(self.server_url, id))
        if not error:
            if __debug__: log(f'got result from {self.server_url} for {id}')
            record = self._record_from_xml(resp.content)
            record.thumbnail = self._thumbnail_for_tind_id(record.tind_id)
            return record
        elif isinstance(error, NoContent):
            if __debug__: log(f'got empty xml content for {id}')
            return TindRecord()
        elif isinstance(error, RateLimitExceeded):
            retry += 1
            if retry > _MAX_SLEEP_CYCLES:
                raise TindError(f'Rate limit exceeded for {self.server_url}')
            else:
                if __debug__: log(f'hit rate limit; pausing {_RATE_LIMIT_SLEEP}s')
                wait(_RATE_LIMIT_SLEEP)
                return self._record_from_server(search_template, id, retry = retry)
        else:
            if __debug__: log(f'got {type(error)} error for {id}')
            raise TindError(f'Problem contacting {self.server_url}: {str(error)}')


    def _record_from_xml(self, xml):
        '''Initialize this record given MARC XML as a string.'''

        record = TindRecord()
        record.tind_server = self.server_url
        # Save the XML internally in case it's useful.
        record._xml = xml

        # Parse the XML.
        if __debug__: log(f'parsing MARC XML {len(xml)} chars long')
        try:
            parser = etree.XMLParser(recover = True)
            tree = etree.fromstring(xml, parser = parser)
        except Exception as ex:
            raise ValueError(f'Bad XML')
        if len(tree) == 0:             # Blank record.
            if __debug__: log(f'blank record -- no values parsed')
            return record
        elements = tree.find(ELEM_RECORD)
        subtitle = None
        for element in elements.findall(ELEM_CONTROLFIELD):
            if element.attrib['tag'] == '001':
                record.tind_id = element.text.strip()
            elif element.attrib['tag'] == '008':
                record.year = element.text[7:11].strip()
                if not record.year.isdigit():
                    record.year = ''

        for element in elements.findall(ELEM_DATAFIELD):
            if element.attrib['tag'] == '250':
                record.edition = element.find(ELEM_SUBFIELD).text.strip()
            elif element.attrib['tag'] == '050':
                record.call_no = ''
                for subfield in element.findall(ELEM_SUBFIELD):
                    record.call_no += subfield.text.strip() + ' '
            elif element.attrib['tag'] == '100':
                for subfield in element.findall(ELEM_SUBFIELD):
                    if subfield.attrib['code'] == 'a':
                        record.main_author = subfield.text.strip()
            elif element.attrib['tag'] == '245':
                for subfield in element.findall(ELEM_SUBFIELD):
                    if subfield.attrib['code'] == 'a':
                        text = subfield.text.strip()
                        # The title sometimes contains the author names too.
                        record.title, record.author = parsed_title_and_author(text)
                    elif subfield.attrib['code'] == 'b':
                        record.subtitle = subfield.text.strip()
                    elif subfield.attrib['code'] == 'c':
                        record.author = subfield.text.strip()
            elif element.attrib['tag'] == '020':
                for subfield in element.findall(ELEM_SUBFIELD):
                    # Value is sometimes of the form "1429224045 (hbk.)"
                    value = subfield.text.split()[0]
                    if value.isdigit():
                        record.isbn_issn.append(value)
            elif element.attrib['tag'] == '300':
                parts = [sub.text.strip() for sub in element.findall(ELEM_SUBFIELD)]
                record.description = ' '.join(parts)
            elif element.attrib['tag'] == '504':
                for subfield in element.findall(ELEM_SUBFIELD):
                    if subfield.attrib['code'] == 'a':
                        record.note = subfield.text.strip()

        # We get author from 245 because in our entries, it's frequently part
        # of the title statement. If it's not, but we got an author from 100
        # use that.  100 only lists first author, but it's better than nothing.
        if record.author:
            if record.author.startswith('by'):
                record.author = record.author[2:].strip()
            elif record.author.startswith('edited by'):
                record.author = record.author[10:].strip()
        elif record.main_author:
            record.author = record.main_author

        # Caltech's TIND database contains some things that are not reading
        # materials per se. The following is an attempt to weed those out.
        if sum([not record.author, not record.year, not record.title]) > 1:
            for field in ['title', 'author', 'year', 'call_no', 'edition']:
                setattr(record. field, None)
            return record

        # Some cleanup work is better left until after we obtain all values.
        record.author      = cleaned(record.author)
        record.title       = cleaned(record.title)
        record.edition     = cleaned(record.edition)
        record.subtitle    = cleaned(record.subtitle)
        record.description = cleaned(record.description)
        record.url         = self._url_for_tind_record(record.tind_id)

        return record


    # Fixme: rename that json template
    def _items_for_tind_id(self, id, retry = 0):
        (resp, error) = net('get', _JSON_USING_TIND_ID.format(self.server_url, id))
        results = []
        if not error:
            if __debug__: log(f'got result from {self.server_url} for {id}')
            try:
                data = json.loads(resp.text)
            except JSONDecodeError as ex:
                raise TindError(f'Malformed result from {self.server_url}: str(ex)')
            except TypeError as ex:
                raise PotionError('Error during item lookup -- please report this.')

            if 'items' not in data:
                if __debug__: log(f'results from server missing "data" key')
                raise TindError(f'Unexpected result from {self.server_url}')
            for item in data['items']:
                results.append(TindItem(barcode     = item.get('barcode', ''),
                                        type        = item.get('item_type', ''),
                                        volume      = item.get('item_volume', ''),
                                        call_number = item.get('call_number', ''),
                                        description = item.get('description', ''),
                                        library     = item.get('library', '',),
                                        location    = item.get('location', ''),
                                        status      = item.get('status', '')))
            return results
        elif isinstance(error, NoContent):
            if __debug__: log(f'got empty json content for {id}')
            return []
        elif isinstance(error, RateLimitExceeded):
            retry += 1
            if retry > _MAX_SLEEP_CYCLES:
                raise TindError(f'Rate limit exceeded for {self.server_url}')
            else:
                if __debug__: log(f'hit rate limit; pausing {_RATE_LIMIT_SLEEP}s')
                wait(_RATE_LIMIT_SLEEP)
                return self._items_for_tind_id(id, retry = retry)
        else:
            if __debug__: log(f'got {type(error)} error for {id}')
            raise TindError(f'Problem contacting {self.server_url}: {str(error)}')


    def _thumbnail_for_tind_id(self, id, retry = 0):
        (resp, error) = net('get', _THUMBNAIL_FOR_TIND_ID.format(self.server_url, id))
        if not error:
            if __debug__: log(f'got thumbnail data for {id}')
            try:
                data = json.loads(resp.text)
            except JSONDecodeError as ex:
                raise TindError(f'Malformed result from {self.server_url}: str(ex)')
            except TypeError as ex:
                raise PotionError('Error getting thumbnail -- please report this.')
            if data:
                if 'big' in data:
                    if __debug__: log(f'thumbnail for {id} is {data["big"]}')
                    return data['big']
                elif 'small' in data:
                    if __debug__: log(f'thumbnail for {id} is {data["small"]}')
                    return data['small']
            if __debug__: log(f'could not find thumbnail for {id}')
            return ''
        elif isinstance(error, NoContent):
            if __debug__: log(f'got empty json content for thumbnail for {id}')
            return ''
        elif isinstance(error, RateLimitExceeded):
            retry += 1
            if retry > _MAX_SLEEP_CYCLES:
                raise TindError(f'Rate limit exceeded for {self.server_url}')
            else:
                if __debug__: log(f'hit rate limit; pausing {_RATE_LIMIT_SLEEP}s')
                wait(_RATE_LIMIT_SLEEP)
                return self._thumbnail_for_tind_id(id, retry = retry)
        else:
            if __debug__: log(f'got {type(error)} error for {id}')
            raise TindError(f'Problem contacting {self.server_url}: {str(error)}')


    def _url_for_tind_record(self, tind_id):
        return f'{self.server_url}/record/{tind_id}'


# Miscellaneous helpers.
# .............................................................................

def cleaned(text):
    '''Mildly clean up the given text string.'''
    if not text:
        return text
    text = text.rstrip('./')
    return text.strip()


def parsed_title_and_author(text):
    '''Extract a title and authors (if present) from the given text string.'''
    title = None
    author = None
    if text.find('/') > 0:
        start = text.find('/')
        title = text[:start].strip()
        author = text[start + 3:].strip()
    elif text.find('[by]') > 0:
        start = text.find('[by]')
        title = text[:start].strip()
        author = text[start + 5:].strip()
    elif text.rfind(', by') > 0:
        start = text.rfind(', by')
        title = text[:start].strip()
        author = text[start + 5:].strip()
    else:
        title = text
    if title.endswith(':'):
        title = title[:-1].strip()
    return title, author
