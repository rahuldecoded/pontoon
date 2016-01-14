from __future__ import absolute_import  # Same name as silme library.
"""
Parser for silme-compatible translation formats.
"""
import codecs
import os
import silme

from collections import OrderedDict
from copy import copy

from silme.format.dtd import FormatParser as DTDParser
from silme.format.ini import FormatParser as IniParser
from silme.format.inc import FormatParser as IncParser
from silme.format.properties import FormatParser as PropertiesParser

from pontoon.sync import SyncError
from pontoon.sync.formats.base import ParsedResource
from pontoon.sync.vcs.models import VCSTranslation


class SilmeEntity(VCSTranslation):
    def __init__(self, silme_object, comments=None, order=0, copy_string=True):
        """
        :param copy_string:
            If True, copy the string from the silme_object. Otherwise,
            self.strings will be an empty dict. Used for creating empty
            copies of translations from source resources.
        """
        self.silme_object = silme_object
        self.comments = comments or []
        self.order = order
        self.last_translator = None
        self.last_update = None

        if copy_string:
            self.strings = {None: self.silme_object.value}
        else:
            self.strings = {}

    @property
    def key(self):
        return self.silme_object.id

    @property
    def source_string(self):
        return self.silme_object.value

    @property
    def source_string_plural(self):
        return ''

    @property
    def fuzzy(self):
        return False

    @fuzzy.setter
    def fuzzy(self, fuzzy):
        pass  # We don't use fuzzy in silme

    @property
    def source(self):
        return []

    def __eq__(self, other):
        return self.key == other.key and self.strings.get(None) == other.strings.get(None)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __nonzero__(self):
        return bool(self.strings)


class SilmeResource(ParsedResource):
    def __init__(self, parser, path, source_resource=None):
        self.parser = parser
        self.path = path
        self.source_resource = source_resource
        self.entities = OrderedDict()  # Preserve entity order.

        # Bug 1193860: unescape quotes in some files
        self.escape_quotes_on = 'mobile/android/base' in path and parser is DTDParser

        # Copy entities from the source_resource if it's available.
        if source_resource:
            for key, entity in source_resource.entities.items():
                self.entities[key] = copy_source_entity(entity)

        try:
            # Only uncomment MOZ_LANGPACK_CONTRIBUTORS if this is a .inc
            # file and a source resource (i.e. it has no source resource
            # itself).
            self.structure = parser.get_structure(read_file(
                path,
                uncomment_moz_langpack=parser is IncParser and not source_resource
            ))
        except IOError:
            # If the file doesn't exist, but we have a source resource,
            # we can keep going, we'll just not have any translations.
            if source_resource:
                return
            else:
                raise

        comments = []
        current_order = 0
        for obj in self.structure:
            if isinstance(obj, silme.core.entity.Entity):

                if self.escape_quotes_on:
                    obj.value = self.unescape_quotes(obj.value)

                entity = SilmeEntity(obj, comments, current_order)
                self.entities[entity.key] = entity
                current_order += 1
                comments = []
            elif isinstance(obj, silme.core.structure.Comment):
                for comment in obj:
                    # Silme groups comments together, so we strip
                    # whitespace and split them up.
                    lines = unicode(comment).strip().split('\n')
                    comments += [line.strip() for line in lines]

    @property
    def translations(self):
        return self.entities.values()

    def escape_quotes(self, value):
        """
        DTD files can use single or double quotes for identifying strings,
        so &quot; and &apos; are the safe bet that will work in both cases.
        """
        value = value.replace('"', '\\&quot;')
        value = value.replace("'", '\\&apos;')
        return value

    def unescape_quotes(self, value):
        value = value.replace('\\&quot;', '"')
        value = value.replace('\\"', '"')
        value = value.replace('\\&apos;', "'")
        value = value.replace("\\'", "'")
        return value

    def save(self, locale):
        """
        Load the source resource, modify it with changes made to this
        Resource instance, and save it over the locale-specific
        resource.
        """
        if self.source_resource is None:
            raise SyncError('Cannot save silme resource {0}: No source resource given.'
                            .format(self.path))

        # Only uncomment MOZ_LANGPACK_CONTRIBUTORS if we have a
        # translation for it
        new_structure = self.parser.get_structure(read_file(
            self.source_resource.path,
            uncomment_moz_langpack=self.entities.get('MOZ_LANGPACK_CONTRIBUTORS', False)
        ))

        # Update translations in the copied resource.
        entities = [
            SilmeEntity(obj) for obj in new_structure if isinstance(obj, silme.core.entity.Entity)
        ]
        for silme_entity in entities:
            key = silme_entity.key

            translated_entity = self.entities.get(key)
            if translated_entity and None in translated_entity.strings:
                translation = translated_entity.strings[None]

                if self.escape_quotes_on:
                    translation = self.escape_quotes(translation)

                new_structure.modify_entity(key, translation)
            else:
                # Remove untranslated entity and following newline
                pos = new_structure.entity_pos(key)
                new_structure.remove_entity(key)

                try:
                    line = new_structure[pos]
                except IndexError:
                    # No newline at end of file
                    continue

                if type(line) == unicode and line.startswith('\n'):
                    line = line[len('\n'):]
                    new_structure[pos] = line
                    if len(line) is 0:
                        new_structure.remove_element(pos)

        # Temporary fix for bug 1236281 until bug 721211 lands
        if self.path.endswith('browser/chrome/browser/browser.properties') and locale.code == 'zh-CN':
            new_entity = silme.core.entity.Entity(
                'browser.startup.homepage',
                'http://start.firefoxchina.cn'
            )
            new_structure.add_entity(new_entity)
            new_structure.add_string('\n')

        # Create parent directory if it doesn't exist.
        try:
            os.makedirs(os.path.dirname(self.path))
        except OSError:
            pass  # Already exists, phew!

        with codecs.open(self.path, 'w', 'utf-8') as f:
            f.write(self.parser.dump_structure(new_structure))


def read_file(path, uncomment_moz_langpack=False):
    """Read the resource at the given path."""
    with codecs.open(path, 'r', 'utf-8') as f:
        # .inc files have a special commented-out entity called
        # MOZ_LANGPACK_CONTRIBUTORS. We optionally un-comment it before
        # parsing so locales can translate it.
        if uncomment_moz_langpack:
            lines = []
            for line in f:
                if line.startswith('# #define MOZ_LANGPACK_CONTRIBUTORS'):
                    line = line[2:]
                lines.append(line)
            content = ''.join(lines)
        else:
            content = f.read()

    return content


def copy_source_entity(entity):
    """
    Copy an entity from a source file to a new SilmeEntity instance.
    The new copy will have an empty strings attribute so that entities
    that are copied but not modified during sync will not be saved in
    the translated resource.
    """
    return SilmeEntity(
        entity.silme_object,
        copy(entity.comments),  # Members are strings, shallow is fine.
        entity.order,
        copy_string=False
    )


def parse(parser, path, source_path=None):
    # TODO: Cache the source resource to avoid re-parsing it a bunch.
    if source_path is not None:
        source_resource = SilmeResource(parser, source_path)
    else:
        source_resource = None

    return SilmeResource(parser, path, source_resource=source_resource)


def parse_properties(path, source_path=None):
    return parse(PropertiesParser, path, source_path)


def parse_ini(path, source_path=None):
    return parse(IniParser, path, source_path)


def parse_inc(path, source_path=None):
    return parse(IncParser, path, source_path)


def parse_dtd(path, source_path=None):
    return parse(DTDParser, path, source_path)
