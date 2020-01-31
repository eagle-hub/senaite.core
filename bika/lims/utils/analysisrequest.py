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
# Copyright 2018-2019 by it's authors.
# Some rights reserved, see README and LICENSE.

import six
import itertools
import os
import tempfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from Products.Archetypes.config import UID_CATALOG
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.utils import _createObjectByType
from Products.CMFPlone.utils import safe_unicode
from email.Utils import formataddr
from zope.interface import alsoProvides
from zope.lifecycleevent import modified

from bika.lims import api
from bika.lims import bikaMessageFactory as _
from bika.lims import logger
from bika.lims.catalog import SETUP_CATALOG
from bika.lims.idserver import renameAfterCreation
from bika.lims.interfaces import IAnalysisRequest
from bika.lims.interfaces import IAnalysisRequestRetest
from bika.lims.interfaces import IAnalysisRequestSecondary
from bika.lims.interfaces import IAnalysisService
from bika.lims.interfaces import IReceived
from bika.lims.interfaces import IRoutineAnalysis
from bika.lims.utils import attachPdf
from bika.lims.utils import changeWorkflowState
from bika.lims.utils import copy_field_values
from bika.lims.utils import createPdf
from bika.lims.utils import encode_header
from bika.lims.utils import tmpID
from bika.lims.utils import to_utf8
from bika.lims.workflow import ActionHandlerPool
from bika.lims.workflow import doActionFor
from bika.lims.workflow import push_reindex_to_actions_pool
from bika.lims.workflow.analysisrequest import AR_WORKFLOW_ID
from bika.lims.workflow.analysisrequest import do_action_to_analyses

try:
    import senaite.queue
    senaite.queue  # noqa
except ImportError:
    USE_QUEUE = False
else:
    USE_QUEUE = True

if USE_QUEUE:
    from senaite.queue.api import is_queue_enabled
    from senaite.queue.queue import queue_set_analyses


def create_analysisrequest(client, request, values, analyses=None,
                           results_ranges=None, prices=None):
    """Creates a new AnalysisRequest (a Sample) object
    :param client: The container where the Sample will be created
    :param request: The current Http Request object
    :param values: A dict, with keys as AnalaysisRequest's schema field names
    :param analyses: List of Services or Analyses (brains, objects, UIDs,
        keywords). Extends the list from values["Analyses"]
    :param results_ranges: List of Results Ranges. Extends the results ranges
        from the Specification object defined in values["Specification"]
    :param prices: Mapping of AnalysisService UID -> price. If not set, prices
        are read from the associated analysis service.
    """
    # Don't pollute the dict param passed in
    values = dict(values.items())

    # Resolve the Service uids of analyses to be added in the Sample. Values
    # passed-in might contain Profiles and also values that are not uids. Also,
    # additional analyses can be passed-in through either values or services
    service_uids = to_services_uids(values=values, services=analyses)

    # Remove the Analyses from values. We will add them manually
    values.update({"Analyses": []})

    # Create the Analysis Request and submit the form
    ar = _createObjectByType('AnalysisRequest', client, tmpID())
    ar.processForm(REQUEST=request, values=values)

    primary = ar.getPrimaryAnalysisRequest()
    if not primary:
        # Can we queue?
        if USE_QUEUE and is_queue_enabled():

            # Queue the assignment of analyses
            queue_set_analyses(ar, request, service_uids, prices=prices,
                               ranges=results_ranges)

        else:
            # Set the analyses manually
            ar.setAnalyses(service_uids, prices=prices, specs=results_ranges)

            # Handle hidden analyses from template and profiles
            # https://github.com/senaite/senaite.core/issues/1437
            # https://github.com/senaite/senaite.core/issues/1326
            apply_hidden_services(ar)

    # Handle rejection reasons
    rejection_reasons = resolve_rejection_reasons(values)
    ar.setRejectionReasons(rejection_reasons)

    # Handle secondary Analysis Request
    primary = ar.getPrimaryAnalysisRequest()
    if primary:
        # Mark the secondary with the `IAnalysisRequestSecondary` interface
        alsoProvides(ar, IAnalysisRequestSecondary)

        # Rename the secondary according to the ID server setup
        renameAfterCreation(ar)

        # Set dates to match with those from the primary
        ar.setDateSampled(primary.getDateSampled())
        ar.setSamplingDate(primary.getSamplingDate())
        ar.setDateReceived(primary.getDateReceived())

        # Force the transition of the secondary to received and set the
        # description/comment in the transition accordingly.
        if primary.getDateReceived():
            primary_id = primary.getId()
            comment = "Auto-received. Secondary Sample of {}".format(primary_id)
            changeWorkflowState(ar, AR_WORKFLOW_ID, "sample_received",
                                action="receive", comments=comment)

            # Mark the secondary as received
            alsoProvides(ar, IReceived)

            # Initialize analyses
            do_action_to_analyses(ar, "initialize")

            # Notify the ar has ben modified
            modified(ar)

            # Reindex the AR
            ar.reindexObject()

            # If rejection reasons have been set, reject automatically
            if rejection_reasons:
                doActionFor(ar, "reject")

            # In "received" state already
            return ar

    # Try first with no sampling transition, cause it is the most common config
    success, message = doActionFor(ar, "no_sampling_workflow")
    if not success:
        doActionFor(ar, "to_be_sampled")

    # If rejection reasons have been set, reject the sample automatically
    if rejection_reasons:
        doActionFor(ar, "reject")

    return ar


def apply_hidden_services(sample):
    """
    Applies the hidden setting to the sample analyses in accordance with the
    settings from its template and/or profiles
    :param sample: the sample that contains the analyses
    """
    hidden = list()

    # Get the "hidden" service uids from the template
    template = sample.getTemplate()
    hidden = get_hidden_service_uids(template)

    # Get the "hidden" service uids from profiles
    profiles = sample.getProfiles()
    hid_profiles = map(get_hidden_service_uids, profiles)
    hid_profiles = list(itertools.chain(*hid_profiles))
    hidden.extend(hid_profiles)

    # Update the sample analyses
    analyses = sample.getAnalyses(full_objects=True)
    analyses = filter(lambda an: an.getServiceUID() in hidden, analyses)
    for analysis in analyses:
        analysis.setHidden(True)


def get_hidden_service_uids(profile_or_template):
    """Returns a list of service uids that are set as hidden
    :param profile_or_template: ARTemplate or AnalysisProfile object
    """
    if not profile_or_template:
        return []
    settings = profile_or_template.getAnalysisServicesSettings()
    hidden = filter(lambda ser: ser.get("hidden", False), settings)
    return map(lambda setting: setting["uid"], hidden)


def to_services_uids(services=None, values=None):
    """
    Returns a list of Analysis Services uids
    :param services: A list of service items (uid, keyword, brain, obj, title)
    :param values: a dict, where keys are AR|Sample schema field names.
    :returns: a list of Analyses Services UIDs
    """
    def to_list(value):
        if not value:
            return []
        if isinstance(value, six.string_types):
            return [value]
        if isinstance(value, (list, tuple)):
            return value
        logger.warn("Cannot convert to a list: {}".format(value))
        return []

    services = services or []
    values = values or {}

    # Merge analyses from analyses_serv and values into one list
    uids = to_list(services) + to_list(values.get("Analyses"))

    # Convert them to a list of service uids
    uids = filter(None, map(to_service_uid, uids))

    # Extend with service uids from profiles
    profiles = to_list(values.get("Profiles"))
    if profiles:
        uid_catalog = api.get_tool(UID_CATALOG)
        for brain in uid_catalog(UID=profiles):
            profile = api.get_object(brain)
            uids.extend(profile.getRawService() or [])

    # Get the service uids without duplicates, but preserving the order
    return list(dict.fromkeys(uids).keys())


def to_service_uid(uid_brain_obj_str):
    """Resolves the passed in element to a valid uid. Returns None if the value
    cannot be resolved to a valid uid
    """
    if api.is_uid(uid_brain_obj_str) and uid_brain_obj_str != "0":
        return uid_brain_obj_str

    if api.is_object(uid_brain_obj_str):
        obj = api.get_object(uid_brain_obj_str)

        if IAnalysisService.providedBy(obj):
            return api.get_uid(obj)

        elif IRoutineAnalysis.providedBy(obj):
            return obj.getServiceUID()

        else:
            logger.error("Type not supported: {}".format(obj.portal_type))
            return None

    if isinstance(uid_brain_obj_str, six.string_types):
        # Maybe is a keyword?
        query = dict(portal_type="AnalysisService", getKeyword=uid_brain_obj_str)
        brains = api.search(query, SETUP_CATALOG)
        if len(brains) == 1:
            return api.get_uid(brains[0])

        # Or maybe a title
        query = dict(portal_type="AnalysisService", title=uid_brain_obj_str)
        brains = api.search(query, SETUP_CATALOG)
        if len(brains) == 1:
            return api.get_uid(brains[0])

    return None


def notify_rejection(analysisrequest):
    """
    Notifies via email that a given Analysis Request has been rejected. The
    notification is sent to the Client contacts assigned to the Analysis
    Request.

    :param analysisrequest: Analysis Request to which the notification refers
    :returns: true if success
    """

    # We do this imports here to avoid circular dependencies until we deal
    # better with this notify_rejection thing.
    from bika.lims.browser.analysisrequest.reject import \
        AnalysisRequestRejectPdfView, AnalysisRequestRejectEmailView

    arid = analysisrequest.getId()

    # This is the template to render for the pdf that will be either attached
    # to the email and attached the the Analysis Request for further access
    tpl = AnalysisRequestRejectPdfView(analysisrequest, analysisrequest.REQUEST)
    html = tpl.template()
    html = safe_unicode(html).encode('utf-8')
    filename = '%s-rejected' % arid
    pdf_fn = tempfile.mktemp(suffix=".pdf")
    pdf = createPdf(htmlreport=html, outfile=pdf_fn)
    if pdf:
        # Attach the pdf to the Analysis Request
        attid = analysisrequest.aq_parent.generateUniqueId('Attachment')
        att = _createObjectByType(
            "Attachment", analysisrequest.aq_parent, attid)
        att.setAttachmentFile(open(pdf_fn))
        # Awkward workaround to rename the file
        attf = att.getAttachmentFile()
        attf.filename = '%s.pdf' % filename
        att.setAttachmentFile(attf)
        att.unmarkCreationFlag()
        renameAfterCreation(att)
        analysisrequest.addAttachment(att)
        os.remove(pdf_fn)

    # This is the message for the email's body
    tpl = AnalysisRequestRejectEmailView(
        analysisrequest, analysisrequest.REQUEST)
    html = tpl.template()
    html = safe_unicode(html).encode('utf-8')

    # compose and send email.
    mailto = []
    lab = analysisrequest.bika_setup.laboratory
    mailfrom = formataddr((encode_header(lab.getName()), lab.getEmailAddress()))
    mailsubject = _('%s has been rejected') % arid
    contacts = [analysisrequest.getContact()] + analysisrequest.getCCContact()
    for contact in contacts:
        name = to_utf8(contact.getFullname())
        email = to_utf8(contact.getEmailAddress())
        if email:
            mailto.append(formataddr((encode_header(name), email)))
    if not mailto:
        return False
    mime_msg = MIMEMultipart('related')
    mime_msg['Subject'] = mailsubject
    mime_msg['From'] = mailfrom
    mime_msg['To'] = ','.join(mailto)
    mime_msg.preamble = 'This is a multi-part MIME message.'
    msg_txt = MIMEText(html, _subtype='html')
    mime_msg.attach(msg_txt)
    if pdf:
        attachPdf(mime_msg, pdf, filename)

    try:
        host = getToolByName(analysisrequest, 'MailHost')
        host.send(mime_msg.as_string(), immediate=True)
    except:
        logger.warning(
            "Email with subject %s was not sent (SMTP connection error)" % mailsubject)

    return True


def create_retest(ar):
    """Creates a retest (Analysis Request) from an invalidated Analysis Request
    :param ar: The invalidated Analysis Request
    :type ar: IAnalysisRequest
    :rtype: IAnalysisRequest
    """
    if not ar:
        raise ValueError("Source Analysis Request cannot be None")

    if not IAnalysisRequest.providedBy(ar):
        raise ValueError("Type not supported: {}".format(repr(type(ar))))

    if ar.getRetest():
        # Do not allow the creation of another retest!
        raise ValueError("Retest already set")

    if not ar.isInvalid():
        # Analysis Request must be in 'invalid' state
        raise ValueError("Cannot do a retest from an invalid Analysis Request"
                         .format(repr(ar)))

    # Open the actions pool
    actions_pool = ActionHandlerPool.get_instance()
    actions_pool.queue_pool()

    # Create the Retest (Analysis Request)
    ignore = ['Analyses', 'DatePublished', 'Invalidated', 'Sample']
    retest = _createObjectByType("AnalysisRequest", ar.aq_parent, tmpID())
    copy_field_values(ar, retest, ignore_fieldnames=ignore)

    # Mark the retest with the `IAnalysisRequestRetest` interface
    alsoProvides(retest, IAnalysisRequestRetest)

    # Assign the source to retest
    retest.setInvalidated(ar)

    # Rename the retest according to the ID server setup
    renameAfterCreation(retest)

    # Copy the analyses from the source
    intermediate_states = ['retracted', 'reflexed']
    for an in ar.getAnalyses(full_objects=True):
        if (api.get_workflow_status_of(an) in intermediate_states):
            # Exclude intermediate analyses
            continue

        nan = _createObjectByType("Analysis", retest, an.getKeyword())

        # Make a copy
        ignore_fieldnames = ['DataAnalysisPublished']
        copy_field_values(an, nan, ignore_fieldnames=ignore_fieldnames)
        nan.unmarkCreationFlag()
        push_reindex_to_actions_pool(nan)

    # Transition the retest to "sample_received"!
    changeWorkflowState(retest, 'bika_ar_workflow', 'sample_received')
    alsoProvides(retest, IReceived)

    # Initialize analyses
    for analysis in retest.getAnalyses(full_objects=True):
        if not IRoutineAnalysis.providedBy(analysis):
            continue
        changeWorkflowState(analysis, "bika_analysis_workflow", "unassigned")

    # Reindex and other stuff
    push_reindex_to_actions_pool(retest)
    push_reindex_to_actions_pool(retest.aq_parent)

    # Resume the actions pool
    actions_pool.resume()
    return retest


def create_partition(analysis_request, request, analyses, sample_type=None,
                     container=None, preservation=None, skip_fields=None,
                     internal_use=True):
    """
    Creates a partition for the analysis_request (primary) passed in
    :param analysis_request: uid/brain/object of IAnalysisRequest type
    :param request: the current request object
    :param analyses: uids/brains/objects of IAnalysis type
    :param sampletype: uid/brain/object of SampleType
    :param container: uid/brain/object of Container
    :param preservation: uid/brain/object of Preservation
    :param skip_fields: names of fields to be skipped on copy from primary
    :return: the new partition
    """
    partition_skip_fields = [
        "Analyses",
        "Attachment",
        "Client",
        "DetachedFrom",
        "Profile",
        "Profiles",
        "RejectionReasons",
        "Remarks",
        "ResultsInterpretation",
        "ResultsInterpretationDepts",
        "Sample",
        "Template",
        "creation_date",
        "id",
        "modification_date",
        "ParentAnalysisRequest",
        "PrimaryAnalysisRequest",
    ]
    if skip_fields:
        partition_skip_fields.extend(skip_fields)
        partition_skip_fields = list(set(partition_skip_fields))

    # Copy field values from the primary analysis request
    ar = api.get_object(analysis_request)
    record = fields_to_dict(ar, partition_skip_fields)

    # Update with values that are partition-specific
    record.update({
        "InternalUse": internal_use,
        "ParentAnalysisRequest": api.get_uid(ar),
    })
    if sample_type is not None:
        record["SampleType"] = sample_type and api.get_uid(sample_type) or ""
    if container is not None:
        record["Container"] = container and api.get_uid(container) or ""
    if preservation is not None:
        record["Preservation"] = preservation and api.get_uid(preservation) or ""

    # Create the Partition
    client = ar.getClient()
    analyses = list(set(map(api.get_object, analyses)))
    services = map(lambda an: an.getAnalysisService(), analyses)

    # Populate the root's ResultsRanges to partitions
    results_ranges = ar.getResultsRange() or []
    partition = create_analysisrequest(client,
                                       request=request,
                                       values=record,
                                       analyses=services,
                                       results_ranges=results_ranges)

    # Reindex Parent Analysis Request
    ar.reindexObject(idxs=["isRootAncestor"])

    # Manually set the Date Received to match with its parent. This is
    # necessary because crar calls to processForm, so DateReceived is not
    # set because the partition has not been received yet
    partition.setDateReceived(ar.getDateReceived())
    partition.reindexObject(idxs="getDateReceived")

    # Force partition to same status as the primary
    status = api.get_workflow_status_of(ar)
    changeWorkflowState(partition, "bika_ar_workflow", status)
    if IReceived.providedBy(ar):
        alsoProvides(partition, IReceived)

    # And initialize the analyses the partition contains. This is required
    # here because the transition "initialize" of analyses rely on a guard,
    # so the initialization can only be performed when the sample has been
    # received (DateReceived is set)
    ActionHandlerPool.get_instance().queue_pool()
    for analysis in partition.getAnalyses(full_objects=True):
        doActionFor(analysis, "initialize")
    ActionHandlerPool.get_instance().resume()
    return partition


def fields_to_dict(obj, skip_fields=None):
    """
    Generates a dictionary with the field values of the object passed in, where
    keys are the field names. Skips computed fields
    """
    data = {}
    obj = api.get_object(obj)
    for field_name, field in api.get_fields(obj).items():
        if skip_fields and field_name in skip_fields:
            continue
        if field.type == "computed":
            continue
        data[field_name] = field.get(obj)
    return data


def resolve_rejection_reasons(values):
    """Resolves the rejection reasons from the submitted values to the format
    supported by Sample's Rejection Reason field
    """
    rejection_reasons = values.get("RejectionReasons")
    if not rejection_reasons:
        return []

    # Predefined reasons selected?
    selected = rejection_reasons[0] or {}
    if selected.get("checkbox") == "on":
        selected = selected.get("multiselection") or []
    else:
        selected = []

    # Other reasons set?
    other = values.get("RejectionReasons.textfield")
    if other:
        other = other[0] or {}
        other = other.get("other", "")
    else:
        other = ""

    # If neither selected nor other reasons are set, return empty
    if any([selected, other]):
        return [{"selected": selected, "other": other}]

    return []
