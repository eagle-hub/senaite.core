# -*- coding: utf-8 -*-
#
# This file is part of SENAITE.CORE.
#
# SENAITE.CORE is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright 2018-2020 by it's authors.
# Some rights reserved, see README and LICENSE.

import sys

from AccessControl import ClassSecurityInfo
from Products.ATContentTypes.lib.historyaware import HistoryAwareMixin
from Products.Archetypes.public import BaseContent
from Products.Archetypes.public import BooleanField
from Products.Archetypes.public import BooleanWidget
from Products.Archetypes.public import FileWidget
from Products.Archetypes.public import ReferenceField
from Products.Archetypes.public import Schema
from Products.Archetypes.public import StringField
from Products.Archetypes.public import StringWidget
from Products.Archetypes.public import registerType
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.utils import safe_unicode
from plone.app.blob.field import FileField as BlobFileField
from zope.interface import implements

from bika.lims import bikaMessageFactory as _
from bika.lims.browser.fields import CoordinateField
from bika.lims.browser.fields import DurationField
from bika.lims.browser.widgets import CoordinateWidget
from bika.lims.browser.widgets import DurationWidget
from bika.lims.browser.widgets.referencewidget import \
    ReferenceWidget as BikaReferenceWidget
from bika.lims.config import PROJECTNAME
from bika.lims.content.bikaschema import BikaSchema
from bika.lims.content.clientawaremixin import ClientAwareMixin
from bika.lims.content.sampletype import SampleTypeAwareMixin
from bika.lims.interfaces import IDeactivable

schema = BikaSchema.copy() + Schema((
    CoordinateField(
        'Latitude',
        schemata='Location',
        widget=CoordinateWidget(
            label=_("Latitude"),
            description=_("Enter the Sample Point's latitude in degrees 0-90, minutes 0-59, seconds 0-59 and N/S indicator"),
        ),
    ),

    CoordinateField(
        'Longitude',
        schemata='Location',
        widget=CoordinateWidget(
            label=_("Longitude"),
            description=_("Enter the Sample Point's longitude in degrees 0-180, minutes 0-59, seconds 0-59 and E/W indicator"),
        ),
    ),

    StringField(
        'Elevation',
        schemata='Location',
        widget=StringWidget(
            label=_("Elevation"),
            description=_("The height or depth at which the sample has to be taken"),
        ),
    ),

    DurationField(
        'SamplingFrequency',
        vocabulary_display_path_bound=sys.maxint,
        widget=DurationWidget(
            label=_("Sampling Frequency"),
            description=_("If a sample is taken periodically at this sample point, enter frequency here, e.g. weekly"),
        ),
    ),

    ReferenceField(
        'SampleTypes',
        required=0,
        multiValued=1,
        allowed_types=('SampleType',),
        relationship='SamplePointSampleType',
        widget=BikaReferenceWidget(
            label=_("Sample Types"),
            description=_("The list of sample types that can be collected "
                          "at this sample point.  If no sample types are "
                          "selected, then all sample types are available."),
            catalog_name='bika_setup_catalog',
            base_query={"is_active": True,
                        "sort_on": "sortable_title",
                        "sort_order": "ascending"},
            showOn=True,
        ),
    ),

    BooleanField(
        'Composite',
        default=False,
        widget=BooleanWidget(
            label=_("Composite"),
            description=_(
                "Check this box if the samples taken at this point are 'composite' "
                "and put together from more than one sub sample, e.g. several surface "
                "samples from a dam mixed together to be a representative sample for the dam. "
                "The default, unchecked, indicates 'grab' samples"),
        ),
    ),

    BlobFileField(
        'AttachmentFile',
        widget=FileWidget(
            label=_("Attachment"),
        ),
    ),
))

schema['description'].widget.visible = True
schema['description'].schemata = 'default'


class SamplePoint(BaseContent, HistoryAwareMixin, ClientAwareMixin,
                  SampleTypeAwareMixin):
    implements(IDeactivable)
    security = ClassSecurityInfo()
    displayContentsTab = False
    schema = schema

    _at_rename_after_creation = True

    def _renameAfterCreation(self, check_auto_id=False):
        from bika.lims.idserver import renameAfterCreation
        renameAfterCreation(self)

    def Title(self):
        return safe_unicode(self.getField('title').get(self)).encode('utf-8')

    def setSampleTypes(self, value, **kw):
        """ For the moment, we're manually trimming the sampletype<>samplepoint
            relation to be equal on both sides, here.
            It's done strangely, because it may be required to behave strangely.
        """
        bsc = getToolByName(self, 'bika_setup_catalog')
        # convert value to objects
        if value and type(value) == str:
            value = [bsc(UID=value)[0].getObject(), ]
        elif value and type(value) in (list, tuple) and type(value[0]) == str:
            value = [bsc(UID=uid)[0].getObject() for uid in value if uid]
        if not type(value) in (list, tuple):
            value = [value, ]
        # Find all SampleTypes that were removed
        existing = self.Schema()['SampleTypes'].get(self)
        removed = existing and [s for s in existing if s not in value] or []
        added = value and [s for s in value if s not in existing] or []
        ret = self.Schema()['SampleTypes'].set(self, value)

        # finally be sure that we aren't trying to set None values here.
        removed = [x for x in removed if x]
        added = [x for x in added if x]

        for st in removed:
            samplepoints = st.getSamplePoints()
            if self in samplepoints:
                samplepoints.remove(self)
                st.setSamplePoints(samplepoints)

        for st in added:
            st.setSamplePoints(list(st.getSamplePoints()) + [self, ])

        return ret


registerType(SamplePoint, PROJECTNAME)
