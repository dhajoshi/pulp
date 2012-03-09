# -*- coding: utf-8 -*-
#
# Copyright © 2011 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

"""
Contains the manager class and exceptions for handling the mappings between
repositories and content units.
"""

import logging
import pymongo
import sys

import pulp.server.content.conduits._common as conduit_common_utils
from pulp.server.content.conduits.unit_import import ImportUnitConduit
import pulp.server.content.loader as plugin_loader
from pulp.server.content.plugins.config import PluginCallConfiguration
import pulp.server.content.types.database as types_db
from pulp.server.db.model.gc_repository import RepoContentUnit
import pulp.server.managers.factory as manager_factory
import pulp.server.exceptions as exceptions
import pulp.server.managers.repo._common as common_utils

# -- constants ----------------------------------------------------------------

_LOG = logging.getLogger(__name__)

# Shadowed here to remove the need for the caller to import RepoContentUnit
# to get access to them
OWNER_TYPE_IMPORTER = RepoContentUnit.OWNER_TYPE_IMPORTER
OWNER_TYPE_USER = RepoContentUnit.OWNER_TYPE_USER

_OWNER_TYPES = (OWNER_TYPE_IMPORTER, OWNER_TYPE_USER)

# Valid sort strings
SORT_TYPE_ID = 'type_id'
SORT_OWNER_TYPE = 'owner_type'
SORT_OWNER_ID = 'owner_id'
SORT_CREATED = 'created'
SORT_UPDATED = 'updated'

_VALID_SORTS = (SORT_TYPE_ID, SORT_OWNER_TYPE, SORT_OWNER_ID, SORT_CREATED, SORT_UPDATED)

SORT_ASCENDING = pymongo.ASCENDING
SORT_DESCENDING = pymongo.DESCENDING

_VALID_DIRECTIONS = (SORT_ASCENDING, SORT_DESCENDING)

# -- manager ------------------------------------------------------------------

class RepoUnitAssociationManager(object):
    """
    Manager used to handle the associations between repositories and content
    units. The functionality provided within assumes the repo and units have
    been created outside of this manager.
    """

    # -- association manipulation ---------------------------------------------

    def associate_unit_by_id(self, repo_id, unit_type_id, unit_id, owner_type, owner_id):
        """
        Creates an association between the given repository and content unit.

        If there is already an association between the given repo and content
        unit, this call has no effect.

        Both repo and unit must exist in the database prior to this call,
        however this call will not verify that for performance reasons. Care
        should be taken by the caller to preserve the data integrity.

        @param repo_id: identifies the repo
        @type  repo_id: str

        @param unit_type_id: identifies the type of unit being added
        @type  unit_type_id: str

        @param unit_id: uniquely identifies the unit within the given type
        @type  unit_id: str

        @param owner_type: category of the caller making the association;
                           must be one of the OWNER_* variables in this module
        @type  owner_type: str

        @param owner_id: identifies the caller making the association, either
                         the importer ID or user login
        @type  owner_id: str

        @raises InvalidType: if the given owner type is not of the valid enumeration
        """

        if owner_type not in _OWNER_TYPES:
            raise exceptions.InvalidType()

        # If the association already exists, no need to do anything else
        spec = {'repo_id' : repo_id,
                'unit_id' : unit_id,
                'unit_type_id' : unit_type_id,
                'owner_type' : owner_type,
                'owner_id' : owner_id,}
        existing_association = RepoContentUnit.get_collection().find_one(spec)
        if existing_association is not None:
            return

        # Create the database entry
        association = RepoContentUnit(repo_id, unit_id, unit_type_id, owner_type, owner_id)
        RepoContentUnit.get_collection().save(association, safe=True)

    def associate_all_by_ids(self, repo_id, unit_type_id, unit_id_list, owner_type, owner_id):
        """
        Creates multiple associations between the given repo and content units.

        See associate_unit_by_id for semantics.

        @param repo_id: identifies the repo
        @type  repo_id: str

        @param unit_type_id: identifies the type of unit being added
        @type  unit_type_id: str

        @param unit_id_list: list of unique identifiers for units within the given type
        @type  unit_id_list: list of str

        @param owner_type: category of the caller making the association;
                           must be one of the OWNER_* variables in this module
        @type  owner_type: str

        @param owner_id: identifies the caller making the association, either
                         the importer ID or user login
        @type  owner_id: str

        @raises InvalidType: if the given owner type is not of the valid enumeration
        """

        # There may be a way to batch this in mongo which would be ideal for a
        # bulk operation like this. But for deadline purposes, this call will
        # simply loop and call the single method.

        for unit_id in unit_id_list:
            self.associate_unit_by_id(repo_id, unit_type_id, unit_id, owner_type, owner_id)

    def associate_from_repo(self, source_repo_id, dest_repo_id, criteria=None):
        """
        Creates associations in a repository based on the contents of a source
        repository. Units from the source repository can be filtered by
        specifying a criteria object.

        The destination repository must have an importer that can support
        the types of units being associated. This is done by analyzing the
        unit list and the importer metadata and takes place before the
        destination repository is called.

        Pulp does not actually perform the associations as part of this call.
        The unit list is determined and passed to the destination repository's
        importer. It is the job of the importer to make the associate calls
        back into Pulp where applicable.

        @param source_repo_id: identifies the source repository
        @type  source_repo_id: str

        @param dest_repo_id: identifies the destination repository
        @type  dest_repo_id: str

        @param criteria: optional; if specified, will filter the units retrieved
                         from the source repository
        @type  criteria: L{Criteria}

        @raises MissingResource: if either of the specified repositories don't exist
        @raises MissingImporter: if the destination repository does not have
                a configured importer
        @raises InvalidType: if one or more units that would be associated
                are of types not supported by the destination repository's importer
        """

        # Validation
        repo_query_manager = manager_factory.repo_query_manager()
        importer_manager = manager_factory.repo_importer_manager()

        source_repo = repo_query_manager.find_by_id(source_repo_id)
        if source_repo is None:
            raise exceptions.MissingResource(source_repo_id)

        dest_repo = repo_query_manager.find_by_id(dest_repo_id)
        if dest_repo is None:
            raise exceptions.MissingResource(dest_repo_id)

        # This will raise MissingImporter if there isn't one, which is the
        # behavior we want this method to exhibit, so just let it bubble up.
        repo_importer = importer_manager.get_importer(dest_repo_id)

        # The docs are incorrect on the list_importer_types call; it actually
        # returns a dict with the types under key "types" for some reason.
        supported_type_ids = plugin_loader.list_importer_types(repo_importer['importer_type_id'])['types']

        # The plugin should get all of the information for each unit, so in case
        # the user specified filters remove them
        if criteria is not None:
            criteria.association_fields = None
            criteria.unit_fields = None

        # Retrieve the units to be associated
        association_query_manager = manager_factory.repo_unit_association_query_manager()
        associate_us = association_query_manager.get_units(source_repo_id, criteria=criteria)

        # If there are no matching units, there's no need to bother the plugin
        if len(associate_us) is 0:
            return

        # Now we can make sure the destination repository's importer is capable
        # of importing the selected units
        associated_unit_type_ids = set([u['unit_type_id'] for u in associate_us])
        unsupported_types = [t for t in associated_unit_type_ids if t not in supported_type_ids]

        if len(unsupported_types) > 0:
            raise exceptions.InvalidType(dest_repo_id, unsupported_types)

        # Convert all of the units into the plugin standard representation
        type_defs = {}
        for def_id in associated_unit_type_ids:
            type_def = types_db.type_definition(def_id)
            type_defs[def_id] = type_def

        transfer_units = []
        for unit in associate_us:
            type_id = unit['unit_type_id']
            u = conduit_common_utils.to_plugin_unit(unit, type_defs[type_id])
            transfer_units.append(u)

        transfer_repo = common_utils.to_transfer_repo(dest_repo)
        transfer_repo.working_dir = common_utils.importer_working_dir(repo_importer['importer_type_id'], dest_repo['id'], mkdir=True)

        # Invoke the importer
        importer_instance, plugin_config = plugin_loader.get_importer_by_id(repo_importer['importer_type_id'])

        call_config = PluginCallConfiguration(plugin_config, repo_importer['config'])
        conduit = ImportUnitConduit(dest_repo_id, repo_importer['id'])

        try:
            importer_instance.import_units(transfer_repo, transfer_units, conduit, call_config)
        except Exception:
            _LOG.exception('Exception from importer [%s] while importing units into repository [%s]' % (repo_importer['importer_type_id'], dest_repo_id))
            raise exceptions.OperationFailed(), None, sys.exc_info()[2]

    def unassociate_unit_by_id(self, repo_id, unit_type_id, unit_id, owner_type, owner_id):
        """
        Removes the association between a repo and the given unit. Only the
        association made by the given owner will be removed. It is possible the
        repo will still have a manually created association will for the unit.

        If no association exists between the repo and unit, this call has no
        effect.

        @param repo_id: identifies the repo
        @type  repo_id: str

        @param unit_type_id: identifies the type of unit being removed
        @type  unit_type_id: str

        @param unit_id: uniquely identifies the unit within the given type
        @type  unit_id: str

        @param owner_type: category of the caller who created the association;
                           must be one of the OWNER_* variables in this module
        @type  owner_type: str

        @param owner_id: identifies the caller who created the association, either
                         the importer ID or user login
        @type  owner_id: str
        """
        spec = {'repo_id' : repo_id,
                'unit_id' : unit_id,
                'unit_type_id' : unit_type_id,
                'owner_type' : owner_type,
                'owner_id' : owner_id,
                }

        unit_coll = RepoContentUnit.get_collection()
        unit_coll.remove(spec, safe=True)

    def unassociate_all_by_ids(self, repo_id, unit_type_id, unit_id_list, owner_type, owner_id):
        """
        Removes the association between a repo and a number of units. Only the
        association made by the given owner will be removed. It is possible the
        repo will still have a manually created association will for the unit.

        @param repo_id: identifies the repo
        @type  repo_id: str

        @param unit_type_id: identifies the type of units being removed
        @type  unit_type_id: str

        @param unit_id_list: list of unique identifiers for units within the given type
        @type  unit_id_list: list of str

        @param owner_type: category of the caller who created the association;
                           must be one of the OWNER_* variables in this module
        @type  owner_type: str

        @param owner_id: identifies the caller who created the association, either
                         the importer ID or user login
        @type  owner_id: str
        """
        spec = {'repo_id' : repo_id,
                'unit_type_id' : unit_type_id,
                'unit_id' : {'$in' : unit_id_list},
                'owner_type' : owner_type,
                'owner_id' : owner_id,
                }

        unit_coll = RepoContentUnit.get_collection()
        unit_coll.remove(spec, safe=True)