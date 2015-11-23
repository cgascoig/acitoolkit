################################################################################
################################################################################
# #
# Copyright (c) 2015 Cisco Systems                                             #
# All Rights Reserved.                                                         #
# #
# Licensed under the Apache License, Version 2.0 (the "License"); you may   #
# not use this file except in compliance with the License. You may obtain   #
# a copy of the License at                                                  #
# #
# http://www.apache.org/licenses/LICENSE-2.0                           #
# #
#    Unless required by applicable law or agreed to in writing, software       #
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT #
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the  #
#    License for the specific language governing permissions and limitations   #
#    under the License.                                                        #
#                                                                              #
################################################################################
"""  ACISearch: Search application for ACI fabrics

    This file contains the main engine for the search tool that handles
    getting the search attrs and value along with the associated objects.
    It can then return a list of objects that match either the keyword, the value
    or the keyword, value pair.
    It runs as a standalone tool in addition, it can be imported as a library
    such as when used by the GUI frontend.
"""
import datetime
import sys
import re
from acitoolkit import BridgeDomain, Context, Contract

from acitoolkit.aciphysobject import Session, Fabric
from acitoolkit.acitoolkitlib import Credentials
from requests import Timeout, ConnectionError


class LoginError(Exception):
    """
    Exception for login errors.
    """
    pass


class Term(object):
    """
    class for the lookup term that contains the key to be looked up
     which can be a class, attr, value, (class, attr), (class, value)
     (attr, value), (class, attr, value)
    And the kind of lookup it should be c, a, v, ca, cv, av, or cav
    """

    def __init__(self, key, term_type, points):

        # todo: make key be fully positional so that (x,,) is different from (,x,)
        self.key = key
        self.type = term_type
        self.points = points

    @classmethod
    def parse_input(cls, strng):
        """
        This method will parse the strng and will create instances of
        the Term object for each search that must occur.
        It will return those Terms in a list

        The assumption is that each strng will be a single fully contained user
        search criteria, i.e. no spaces.
        :param strng:
        """
        # look for #, :, =, and *.  First character is implied to be * if it is not
        # any of the others
        any_escape = '\\*'
        class_escape = '#'
        attr_escape = '@'
        value_escape = '='

        if strng[0] not in [any_escape, class_escape, attr_escape, value_escape]:
            new_string = '*' + strng
        else:
            new_string = strng

        (class_valid, class_str) = cls.build_search_term(class_escape, new_string)
        (attr_valid, attr_str) = cls.build_search_term(attr_escape, new_string)
        (value_valid, value_str) = cls.build_search_term(value_escape, new_string)
        (any_valid, any_str) = cls.build_search_term(any_escape, new_string)

        result = []
        if class_valid and attr_valid and value_valid:
            term = cls((class_str, attr_str, value_str), 'cav', 8)
            result.append(term)
            return result

        if class_valid and attr_valid:
            if any_valid:
                term = cls((class_str, attr_str, any_str), 'cav', 6)
                result.append(term)
                return result
            term = cls((class_str, attr_str), 'ca', 4)
            result.append(term)
            return result

        if class_valid and value_valid:
            if any_valid:
                term = cls((class_str, any_str, value_str), 'cav', 6)
                result.append(term)
                return result
            term = cls((class_str, value_str), 'cv', 4)
            result.append(term)
            return result

        if class_valid and any_valid:
            term = cls((class_str, any_str), 'ca', 3)
            result.append(term)
            term = cls((class_str, any_str), 'cv', 3)
            result.append(term)
            return result

        if attr_valid and value_valid:
            if any_valid:
                term = cls((any_str, attr_str, value_str), 'cav', 6)
                result.append(term)
                return result
            term = cls((attr_str, value_str), 'av', 4)
            result.append(term)
            return result

        if attr_valid and any_valid:
            term = cls((any_str, attr_str), 'ca', 3)
            result.append(term)
            term = cls((attr_str, any_str), 'av', 3)
            result.append(term)
            return result

        if value_valid and any_valid:
            term = cls((any_str, value_str), 'cv', 3)
            result.append(term)
            term = cls((any_str, value_str), 'av', 3)
            result.append(term)
            return result

        if class_valid:
            term = cls(class_str, 'c', 2)
            result.append(term)
            return result

        if attr_valid:
            term = cls(attr_str, 'a', 2)
            result.append(term)
            return result

        if value_valid:
            term = cls(value_str, 'v', 2)
            result.append(term)
            return result

        if any_valid:
            term = cls(any_str, 'c', 1)
            result.append(term)
            term = cls(any_str, 'a', 1)
            result.append(term)
            term = cls(any_str, 'v', 1)
            result.append(term)
        return result

    @staticmethod
    def build_search_term(escape_char, strng):
        """
        Given an escape_char character and a string, it will return
        whether a string that starts with that escape_char is in the string
        and will also return the value of the string after the escape_char.
        :param strng:
        :param escape_char:
        """

        mid = re.search(escape_char + '([^@=#*]+)[@=#\*]', strng)
        end = re.search(escape_char + '([^@=#*]*)$', strng)

        valid = False
        term_string = ''
        if mid:
            term_string = mid.group(1)
            valid = len(term_string) > 0
        elif end:
            term_string = end.group(1)
            valid = len(term_string) > 0

        return valid, term_string


class SearchIndexLookup(object):
    """
    This class contains will index objects by class, attr and value.  A unique ID is what is stored in the index.
    return a list of unique IDs in response to a search string.
    """

    def __init__(self):
        self.by_attr = {}
        self.by_value = {}
        self.by_class = {}
        self.by_attr_value = {}
        self.by_class_value = {}
        self.by_class_attr = {}
        self.by_class_attr_value = {}

    def _index_searchables(self, searchables):

        """
        index all the searchable items by attr, value, and class
        :param searchables: List of searchable objects
        """
        t1 = datetime.datetime.now()
        count = 0
        self.by_attr = {}
        self.by_value = {}
        self.by_attr_value = {}
        self.by_class = {}
        self.by_class_value = {}
        self.by_class_attr = {}
        self.by_class_attr_value = {}

        # index searchables by keyword, value and keyword/value
        for searchable in searchables:
            count += 1
            if count % 1000 == 0:
                print count
            atk_class = searchable.object_class
            atk_attrs = searchable.attr
            atk_values = searchable.value
            atk_attr_values = searchable.attr_value
            uid = searchable.primary.get_attributes()['dn']
            # index by_class
            if atk_class not in self.by_class:
                self.by_class[atk_class] = set([])
            self.by_class[atk_class].add(uid)

            # index by attr and by attr, class
            for atk_attr in atk_attrs:
                if atk_attr not in self.by_attr:
                    self.by_attr[atk_attr] = set([])
                if (atk_class, atk_attr) not in self.by_class_attr:
                    self.by_class_attr[(atk_class, atk_attr)] = set([])

                self.by_attr[atk_attr].add(uid)
                self.by_class_attr[(atk_class, atk_attr)].add(uid)

            # index by values and by value, class
            for atk_value in atk_values:
                if atk_value not in self.by_value:
                    self.by_value[atk_value] = set([])
                if (atk_class, atk_value) not in self.by_class_value:
                    self.by_class_value[(atk_class, atk_value)] = set([])

                self.by_value[atk_value].add(uid)
                self.by_class_value[(atk_class, atk_value)].add(uid)

            # index by attr & value and by class, attr, value
            for atk_attr_value in atk_attr_values:
                if atk_attr_value not in self.by_attr_value:
                    self.by_attr_value[atk_attr_value] = set([])
                self.by_attr_value[atk_attr_value].add(uid)
                (atk_attr, atk_value) = atk_attr_value
                if (atk_class, atk_attr, atk_value) not in self.by_class_attr_value:
                    self.by_class_attr_value[(atk_class, atk_attr, atk_value)] = set([])

                self.by_class_attr_value[(atk_class, atk_attr, atk_value)].add(uid)

        t2 = datetime.datetime.now()
        print 'elapsed time', t2 - t1

    def add_atk_objects(self, root):
        """
        Will add all the objects recursively from the root down into the index
        :param root:
        """
        searchables = root.get_searchable()
        self._index_searchables(searchables)

    def search(self, term_string):
        """
        This will do the actual search.  The data must already be loaded and indexed before this is invoked.
        :param term_string: string that contains all the terms.
        """
        t1 = datetime.datetime.now()
        terms = self._get_terms(term_string)
        # terms = ['#AppProfile:name=APP1', 'leaf']
        results = []
        for term in terms:
            if term.type == 'cav':
                results.append((term, self.by_class_attr_value.get(term.key)))

            if term.type == 'ca':
                results.append((term, self.by_class_attr.get(term.key)))
            if term.type == 'cv':
                results.append((term, self.by_class_value.get(term.key)))
            if term.type == 'av':
                results.append((term, self.by_attr_value.get(term.key)))

            if term.type == 'c':
                results.append((term, self.by_class.get(term.key)))
            if term.type == 'a':
                results.append((term, self.by_attr.get(term.key)))
            if term.type == 'v':
                results.append((term, self.by_value.get(term.key)))

        results2 = self._rank_results(results)
        t2 = datetime.datetime.now()
        print 'elapsed time', t2 - t1
        return results2

    @staticmethod
    def _get_terms(term_string):
        terms = term_string.strip().split(' ')
        result = []
        for term in terms:
            result.extend(Term.parse_input(term))
        return result

    def _rank_results(self, unranked_results):
        """
        Will assign a score to each result item according to how relevant it is.  Higher numbers are more relevant.
        unranked_results is a list of results.  Each of the results is a tuple of the matching term and a list of
        items that have that term.
        :param unranked_results:
        """
        master_items = set()
        for results in unranked_results:
            if results[1] is not None:
                master_items = master_items | results[1]

        self.ranked_items = {}
        for item in master_items:
            # self.ranked_items[item] = [0, 0, set()]  # score, sub-score, matching terms
            self.ranked_items[item] = {'pscore': 0, 'sscore': 0, 'terms': set()}  # score, sub-score, matching terms

        # calculate score -
        # primary score is based on the sum of the specificity of each match
        #   cav == 4
        #   ca, cv, av = 2
        #   c, a, v = 1
        #
        # For example, if there was a 'cav' match and an 'av' match, then the score would be 4 + 2 = 6
        #
        for atk_obj in master_items:
            for result in unranked_results:
                if result[1] is not None:
                    if atk_obj in result[1]:
                        self.ranked_items[atk_obj]['pscore'] += result[0].points
                        self.ranked_items[atk_obj]['terms'].add(str(result[0].key))

        # now score
        # sub-score is one point for any term that is not a primiary hit, but is a secondary hit
        # a primary hit is one where the term directly found the item
        # a secondary hit is one where the term found an item in the heirarchy of the primary item
        #
        # The max sub-score is cumulative, i.e. a sub-score can be greater than the number of terms

        print 'end ranking'
        resp = []
        count = 0
        for result in sorted(self.ranked_items,
                             key=lambda x: (-self.ranked_items[x]['pscore'], -self.ranked_items[x]['sscore'], x)):

            count += 1
            record = {'pscore': self.ranked_items[result]['pscore'],
                      'sscore': self.ranked_items[result]['sscore'],
                      'terms': list(self.ranked_items[result]['terms']),
                      'uid': result}
            resp.append(record)
            if count >= 100:
                break

        return resp, len(self.ranked_items)


class SearchObjectStore(object):
    """
    Will store the objects in a text format by unique ID.  They can then be retrieved by the same ID
    in either a summary format or detailed format.

    This class will also cross-reference the objects if possible.
    """

    def __init__(self):
        self.attrs = []
        self.values = []
        self.ranked_items = {}
        self.map_class = {}  # index of objects by their class
        self.object_directory = {}

    def add_atk_objects(self, root):
        """
        Will add acitoolkit objects to object store and will cross-reference them
        :param root:
        :return:
        """
        self._create_object_directory(root)
        self._cross_reference_objects()

    def _create_object_directory(self, root):
        """
        Will create a dictionary of all the atk objects indexed by their dn.
        :param root:
        :return:
        """
        self.object_directory = {}
        self._add_dir_entry(root)

    def _add_dir_entry(self, root):
        """
        Will recursively add each object and its children to directory
        :param root:
        :return:
        """
        attrs = root.get_attributes()
        if 'dn' not in attrs:
            print 'no guid'
        guid = attrs['dn']
        if guid in self.object_directory:
            print 'Duplicate guid', guid

        self.object_directory[guid] = root
        for child in root.get_children():
            self._add_dir_entry(child)

        # build class map
        if root.__class__.__name__ not in self.map_class:
            self.map_class[root.__class__.__name__] = []

        self.map_class[root.__class__.__name__].append(root)

    def _cross_reference_objects(self):
        """
        Will go through various objects and add gui cross reference related information
        such as adding switches to a tenant object
        :return:
        """

        # map tenants to switches
        for tenant in self.map_class.get('Tenant', {}):
            for concrete_bd in self.map_class.get('ConcreteBD', {}):
                ctenant_name = concrete_bd.attr['tenant']
                if ctenant_name == tenant.name:
                    switch = concrete_bd.get_parent()
                    self._add_relation('switches', switch, tenant)
                    self._add_relation('tenants', tenant, switch)

        for bridge_domain in self.map_class.get('BridgeDomain'):
            for concrete_bd in self.map_class.get('ConcreteBD',{}):
                if ':' in concrete_bd.attr['name']:
                    cbd_name = concrete_bd.attr['name'].split(':')[-1]
                else:
                    cbd_name = concrete_bd.attr['name']

                if cbd_name == bridge_domain.name and concrete_bd.attr['tenant'] == bridge_domain.get_parent().name:
                    switch = concrete_bd.get_parent()
                    self._add_relation('switches', switch, bridge_domain)
                    self._add_relation('bridge domains', bridge_domain, switch)

                    self._add_relation('concrete BD', concrete_bd, bridge_domain)
                    self._add_relation('logical BD', bridge_domain, concrete_bd)

            relations = bridge_domain._relations
            for relation in relations:
                if isinstance(relation.item, Context):
                    self._add_relation('context', relation.item, bridge_domain)
                    self._add_relation('bridge domains', bridge_domain, relation.item)

        for context in self.map_class['Context']:
            for concrete_bd in self.map_class.get('ConcreteBD', {}):
                ccontext_name = concrete_bd.attr['context']
                if ccontext_name == context.name and concrete_bd.attr['tenant'] == context.get_parent().name:
                    switch = concrete_bd.get_parent()
                    self._add_relation('switches', switch, context)
                    self._add_relation('contexts', context, switch)

        for ep in self.map_class.get('Endpoint', {}):
            epg = ep.get_parent()
            app_profile = epg.get_parent()
            tenant = app_profile.get_parent()
            self._add_relation('endpoints', ep, app_profile)
            self._add_relation('endpoints', ep, tenant)
            self._add_relation('tenant', tenant, ep)
            self._add_relation('app profile', app_profile, ep)

        for epg in self.map_class['EPG']:
            relations = epg._relations
            for relation in relations:
                if isinstance(relation.item, Contract):
                    if relation.relation_type == 'consumed':
                        self._add_relation('consumes', relation.item, epg)
                        self._add_relation('consumed by', epg, relation.item)
                    elif relation.relation_type == 'provided':
                        self._add_relation('provides', relation.item, epg)
                        self._add_relation('provided by', epg, relation.item)
                    else:
                        print 'unexpected relation type', relation.relation_type
                if isinstance(relation.item, BridgeDomain):
                    self._add_relation('bridge domain', relation.item, epg)
                    self._add_relation('epgs', epg, relation.item)

    @staticmethod
    def _add_relation(relationship_type, child_obj, parent_obj):
        """
        Will add child_obj to parent_obj with the relationship type
        :param child_obj:
        :param parent_obj:
        :return:
        """
        if 'gui_x_reference' not in parent_obj.__dict__:
            parent_obj.gui_x_reference = {}

        if isinstance(child_obj, BridgeDomain) or isinstance(child_obj, Context):
            child_name = child_obj.get_parent().name + ':' + child_obj.name
        else:
            child_name = child_obj.name

        record = {'class': child_obj.__class__.__name__, 'name': child_name, 'dn': child_obj.dn}
        if relationship_type not in parent_obj.gui_x_reference:
            parent_obj.gui_x_reference[relationship_type] = []

        for existing_record in parent_obj.gui_x_reference[relationship_type]:
            if record['dn'] == existing_record['dn']:
                return
        parent_obj.gui_x_reference[relationship_type].append(record)

    def get_object_info(self, obj_dn):
        """
        Will return dictionary containing all of the information in the
        object.  This information includes all the attributes as well as interesting relationships.
        The relationships include those explicitly in the APIC as well as others that
        are interesting from a model navigation perspective.
        :rtype : dict
        :param obj_dn:
        :return: result
        """
        result = {}
        atk_obj = self.object_directory[obj_dn]
        attr = atk_obj.get_attributes()

        result['properties'] = {'class': atk_obj.__class__.__name__, 'name': attr['name'], 'dn': obj_dn}

        result['attributes'] = atk_obj.get_attributes()

        if atk_obj.get_parent() is not None:
            parent = atk_obj.get_parent().get_attributes()['name']
            parent_dn = atk_obj.get_parent().get_attributes()['dn']
            parent_class = atk_obj.get_parent().__class__.__name__
            result['parent'] = {'class': parent_class, 'name': parent, 'dn': parent_dn}

        children = atk_obj.get_children()
        result['children'] = {}
        for child in children:
            child_class = child.__class__.__name__
            if child_class not in result['children']:
                result['children'][child_class] = []

            result['children'][child_class].append({'class': child.__class__.__name__,
                                                    'name': child.get_attributes()['name'],
                                                    'dn': child.get_attributes()['dn']})

        if 'gui_x_reference' in atk_obj.__dict__:
            result['relations'] = atk_obj.gui_x_reference

        return result

    def get_by_uids_short(self, uids):
        """
        Will return a dictionary indexed by uid, where each entry is a dictionary holding the class and name
        of the object refereced by the uid.
        :param uids: list of UIDs
        """
        result = {}
        for uid in uids:
            atk_obj = self.object_directory[uid]
            record = {'class': atk_obj.__class__.__name__,
                      'name': atk_obj.get_attributes()['name'],
                      'dn': atk_obj.get_attributes()['dn']}
            result[uid] = record

        return result


class SearchSession(object):
    """
    The primary search object that holds all the methods for building the search index as well as querying it.
    """

    def __init__(self, args=None):
        """
        Will load in all of the search objects and create
        an index by attr, value, and class and all combinations.
        """
        self._session = None
        self.args = None
        self.timeout = None

        if args:
            self.set_login_credentials(args)

    def set_login_credentials(self, args, timeout=2):
        """
        Sets the login credentials for the APIC

        :rtype : None
        :param args: An instance containing the APIC credentials.  Expected to
                     have the following instance variables; url, login, and
                     password.
        :param timeout:  Optional integer argument that indicates the timeout
                         value in seconds to use for APIC communication.
                         Default value is 2.
        """
        self.args = args
        self.timeout = timeout
        self._clear_switch_info()

    @property
    def session(self):
        """
        session property will return an active session that has been logged in
        If a login had not previously occurred, it will proactively login first.
        :return: Session
        """
        if self._session is None:
            if self.args is not None:
                if self.args.login is not None:
                    self._session = Session(self.args.url, self.args.login, self.args.password)
                    resp = self.session.login(self.timeout)
                else:
                    raise LoginError
            else:
                raise LoginError
            if not resp.ok:
                raise LoginError
        return self._session

    def _clear_switch_info(self):
        """
        This will clear out the switch info to force a reload of the switch information from the APIC.
        :return:
        """
        self._session = None


class SearchDb(object):
    """
    This class will pull it all together for the search GUI
    """
    def __init__(self):
        self.initialized = False
        self.session = SearchSession()
        self.index = SearchIndexLookup()
        self.store = SearchObjectStore()

    def load_db(self, args):
        self.session.set_login_credentials(args)
        fabric = Fabric.get(self.session.session)[0]
        fabric.populate_children(deep=True, include_concrete=True)

        self.index.add_atk_objects(fabric)
        self.store.add_atk_objects(fabric)
        self.initialized = True

    def search(self, terms):
        (results, total) = self.index.search(terms)
        for result in results:
            short_record = self.store.get_by_uids_short([result['uid']])
            result['name'] = short_record[result['uid']]['name']
            result['class'] = short_record[result['uid']]['class']
        return results, total


def main():
    """
    Main execution path when run from the command line
    """
    # Get all the arguments
    description = 'Search tool for APIC.'
    creds = Credentials('apic', description)
    creds.add_argument('--force',
                       action="store_true",
                       default=False,
                       help='Force a rebuild of the search index')

    args = creds.get()
    print args
    # load all objects
    session = SearchSession(args)
    try:
        fabric = Fabric.get(session.session)[0]
    except (LoginError, Timeout, ConnectionError):
        print '%% Could not login to APIC'
        sys.exit(0)

    fabric.populate_children(deep=True, include_concrete=True)

    index = SearchIndexLookup()
    store = SearchObjectStore()

    index.add_atk_objects(fabric)
    store.add_atk_objects(fabric)

    uids = index.search(args.find)
    result = store.get_by_uids_short(uids)

    count = 0
    for res in result:
        count += 1
        print res


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
