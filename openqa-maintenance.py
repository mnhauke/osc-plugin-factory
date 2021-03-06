#!/usr/bin/python
# Copyright (c) 2015-2017 SUSE LLC
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import os.path as opa
import re
import sys
from datetime import date
import md5

import simplejson as json
from simplejson import JSONDecodeError

import logging
import requests
from collections import namedtuple
from pprint import pformat
try:
    from xml.etree import cElementTree as ET
except ImportError:
    import cElementTree as ET

import osc.conf
import osc.core

from osclib.comments import CommentAPI

import ReviewBot

from openqa_client.client import OpenQA_Client
from openqa_client import exceptions as openqa_exceptions

Package = namedtuple('Package', ('name', 'version', 'release'))

pkgname_re = re.compile(r'(?P<name>.+)-(?P<version>[^-]+)-(?P<release>[^-]+)\.(?P<arch>[^.]+)\.rpm')

# QA Results
QA_UNKNOWN = 0
QA_INPROGRESS = 1
QA_FAILED = 2
QA_PASSED = 3

comment_marker_re = re.compile(r'<!-- openqa state=(?P<state>done|seen)(?: result=(?P<result>accepted|declined))? -->')

logger = None

request_name_cache = {}

# old stuff, for reference
#    def filterchannel(self, apiurl, prj, packages):
#        """ filter list of package objects to only include those actually released into prj"""
#
#        prefix = 'SUSE:Updates:'
#        logger.debug(prj)
#        if not prj.startswith(prefix):
#            return packages
#
#        channel = prj[len(prefix):].replace(':', '_')
#
#        url = osc.core.makeurl(apiurl, ('source',  'SUSE:Channels', channel, '_channel'))
#        root = ET.parse(osc.core.http_GET(url)).getroot()
#
#        package_names = set([p.name for p in packages])
#        in_channel = set([p.attrib['name'] for p in root.iter('binary') if p.attrib['name'] in package_names])
#
#        return [p for p in packages if p.name in in_channel]

data_path = opa.abspath(opa.dirname(sys.argv[0]))

with open(opa.join(data_path, "data/kgraft.json"), 'r') as f:
    KGRAFT_SETTINGS = json.load(f)

with open(opa.join(data_path, "data/repos.json"), 'r') as f:
    TARGET_REPO_SETTINGS = json.load(f)


class Update(object):

    def __init__(self, settings):
        self._settings = settings
        self._settings['_NOOBSOLETEBUILD'] = '1'

    def settings(self, src_prj, dst_prj, packages, req):
        s = self._settings.copy()

        # start with a colon so it looks cool behind 'Build' :/
        s['BUILD'] = ':' + req.reqid + '.' + self.request_name(req)
        s['INCIDENT_REPO'] = '%s/%s/%s/' % (self.repo_prefix(), src_prj.replace(':', ':/'), dst_prj.replace(':', '_'))

        return s

    def calculate_lastest_good_updates(self, openqa, settings):
        # not touching anything by default
        pass

    # take the first package name we find - often enough correct
    def request_name(self, req):
        if req.reqid not in request_name_cache:
            request_name_cache[req.reqid] = self._request_name(req)
        return request_name_cache[req.reqid]

    def _request_name(self, req):
        for action in req.get_actions('maintenance_release'):
            if action.tgt_package.startswith('patchinfo'):
                continue
            url = osc.core.makeurl(
                req.apiurl,
                ('source', action.src_project, action.src_package, '_link'))
            root = ET.parse(osc.core.http_GET(url)).getroot()
            if root.attrib.get('cicount'):
                continue
            return action.tgt_package

        return 'unknown'


class SUSEUpdate(Update):

    def repo_prefix(self):
        return 'http://download.suse.de/ibs'

    # we take requests that have a kgraft-patch package as kgraft patch (suprise!)
    @staticmethod
    def kgraft_target(req):
        target = None
        action = None
        skip = False
        pattern = re.compile(r"kgraft-patch-([^.]+)\.")

        if req:
            for a in req.actions:
                if a.src_package.startswith("kernel-"):
                    skip = True
                    break
                match = re.match(pattern, a.src_package)
                if match:
                    target = match.group(1)
                    action = a
        if skip:
            return None, None

        return target, action

    @staticmethod
    def parse_kgraft_version(kgraft_target):
        return kgraft_target.lstrip('SLE').split('_')[0]

    @staticmethod
    def kernel_target(req):
        if req:
            for a in req.actions:
                # kernel incidents have kernel-source package (suprise!)
                if a.src_package.startswith('kernel-source'):
                    return True, a
        return None, None

    def settings(self, src_prj, dst_prj, packages, req=None):
        settings = super(SUSEUpdate, self).settings(src_prj, dst_prj, packages, req)

        # special handling for kgraft and kernel incidents
        if settings['FLAVOR'] in ('KGraft', 'Server-DVD-Incidents-Kernel'):
            kgraft_target, action = self.kgraft_target(req)
        # Server-DVD-Incidents-Incidents handling
        if settings['FLAVOR'] == 'Server-DVD-Incidents-Kernel':
            kernel_target, kaction = self.kernel_target(req)
            if kernel_target or kgraft_target:
                # incident_id as part of BUILD
                if kgraft_target:
                    incident_id = re.match(r".*:(\d+)$", action.src_project).group(1)
                    name = '.kgraft.'
                    settings['KGRAFT'] = '1'
                else:
                    incident_id = re.match(r".*:(\d+)$", kaction.src_project).group(1)
                    name = '.kernel.'

                # discard jobs without 'start'
                settings['start'] = True
                settings['BUILD'] = ':' + req.reqid + name + incident_id
                if kgraft_target:
                    settings['VERSION'] = self.parse_kgraft_version(kgraft_target)
        # ignore kgraft patches without defined target
        # they are actually only the base for kgraft
        if settings['FLAVOR'] == 'KGraft' and kgraft_target and kgraft_target in KGRAFT_SETTINGS:
            incident_id = re.match(r".*:(\d+)$", action.src_project).group(1)
            settings.update(KGRAFT_SETTINGS[kgraft_target])
            settings['BUILD'] = ':' + req.reqid + '.kgraft.' + incident_id
            settings['MAINT_UPDATE_RRID'] = action.src_project + ':' + req.reqid

        return settings


class openSUSEUpdate(Update):

    def calculate_lastest_good_updates(self, openqa, settings):
        j = openqa.openqa_request(
            'GET', 'jobs',
            {
                'distri': settings['DISTRI'],
                'version': settings['VERSION'],
                'arch': settings['ARCH'],
                'flavor': 'Updates',
                'scope': 'current',
                'limit': 100  # this needs increasing if we ever get *monster* coverage for released updates
            })['jobs']
        # check all publishing jobs per build and reject incomplete builds
        builds = {}
        for job in j:
            if 'PUBLISH_HDD_1' not in job['settings']:
                continue
            if job['result'] == 'passed' or job['result'] == 'softfailed':
                builds.setdefault(job['settings']['BUILD'], 'passed')
            else:
                builds[job['settings']['BUILD']] = 'failed'

        # take the last one passing completely
        lastgood_prefix = 0
        lastgood_suffix = 0
        for build, status in builds.items():
            if status == 'passed':
                try:
                    prefix = int(build.split('-')[0])
                    suffix = int(build.split('-')[1])
                    if prefix > lastgood_prefix:
                        lastgood_prefix = prefix
                        lastgood_suffix = suffix
                    elif prefix == lastgood_prefix and suffix > lastgood_suffix:
                        lastgood_suffix = suffix
                except ValueError:
                    continue

        if lastgood_prefix:
            settings['LATEST_GOOD_UPDATES_BUILD'] = "%d-%d" % (lastgood_prefix, lastgood_suffix)

    def repo_prefix(self):
        return 'http://download.opensuse.org/repositories'

    def settings(self, src_prj, dst_prj, packages, req=None):
        settings = super(openSUSEUpdate, self).settings(src_prj, dst_prj, packages, req)

        # openSUSE:Maintenance key
        settings['IMPORT_GPG_KEYS'] = 'gpg-pubkey-b3fd7e48-5549fd0f'
        settings['ZYPPER_ADD_REPO_PREFIX'] = 'incident'

        if packages:
            # XXX: this may fail in various ways
            # - conflicts between subpackages
            # - added packages
            # - conflicts with installed packages (e.g sendmail vs postfix)
            settings['INSTALL_PACKAGES'] = ' '.join(set([p.name for p in packages]))
            settings['VERIFY_PACKAGE_VERSIONS'] = ' '.join(
                ['{} {}-{}'.format(p.name, p.version, p.release) for p in packages])

        settings['ZYPPER_ADD_REPOS'] = settings['INCIDENT_REPO']
        settings['ADDONURL'] = settings['INCIDENT_REPO']

        settings['WITH_MAIN_REPO'] = 1
        settings['WITH_UPDATE_REPO'] = 1

        return settings


class TestUpdate(openSUSEUpdate):

    def settings(self, src_prj, dst_prj, packages, req=None):
        settings = super(TestUpdate, self).settings(src_prj, dst_prj, packages, req)

        settings['IMPORT_GPG_KEYS'] = 'testkey'

        return settings


PROJECT_OPENQA_SETTINGS = {}

with open(opa.join(data_path, "data/incidents.json"), 'r') as f:
    for i, j in json.load(f).items():
        if i.startswith('SUSE'):
            PROJECT_OPENQA_SETTINGS[i] = [SUSEUpdate(k) for k in j]
        elif i.startswith('openSUSE'):
            PROJECT_OPENQA_SETTINGS[i] = [openSUSEUpdate(k) for k in j]
        else:
            PROJECT_OPENQA_SETTINGS[i] = [TestUpdate(k) for k in j]


class OpenQABot(ReviewBot.ReviewBot):

    """ check ABI of library packages
    """

    def __init__(self, *args, **kwargs):
        ReviewBot.ReviewBot.__init__(self, *args, **kwargs)

        self.force = False
        self.openqa = None
        self.commentapi = CommentAPI(self.apiurl)
        self.update_test_builds = dict()

    def gather_test_builds(self):
        for prj, u in TARGET_REPO_SETTINGS[self.openqa.baseurl].items():
            buildnr = 0
            cjob = 0
            for j in self.jobs_for_target(u):
                # avoid going backwards in job ID
                if cjob > int(j['id']):
                    continue
                buildnr = j['settings']['BUILD']
                cjob = int(j['id'])
            self.update_test_builds[prj] = buildnr

    # reimplemention from baseclass
    def check_requests(self):

        # first calculate the latest build number for current jobs
        self.gather_test_builds()

        self.pending_target_repos = set()

        started = []
        # then check progress on running incidents
        for req in self.requests:
            # just patch apiurl in to avoid having to pass it around
            req.apiurl = self.apiurl
            jobs = self.request_get_openqa_jobs(req, incident=True, test_repo=True)
            ret = self.calculate_qa_status(jobs)
            if ret != QA_UNKNOWN:
                started.append(req)

        all_requests = self.requests
        self.requests = started
        ReviewBot.ReviewBot.check_requests(self)

        self.requests = all_requests

        skipped_one = False
        # now make sure the jobs are for current repo
        for prj, u in TARGET_REPO_SETTINGS[self.openqa.baseurl].items():
            if prj in self.pending_target_repos:
                skipped_one = True
                continue
            self.trigger_build_for_target(prj, u)

        # do not schedule new incidents unless we finished
        # last wave
        if skipped_one:
            return

        ReviewBot.ReviewBot.check_requests(self)

    def check_action_maintenance_release(self, req, a):
        # we only look at the binaries of the patchinfo
        if a.src_package != 'patchinfo':
            return None

        if a.tgt_project not in PROJECT_OPENQA_SETTINGS:
            self.logger.warn("not handling %s" % a.tgt_project)
            return None

        packages = []
        patch_id = None
        # patchinfo collects the binaries and is build for an
        # unpredictable architecture so we need iterate over all
        url = osc.core.makeurl(
            self.apiurl,
            ('build', a.src_project, a.tgt_project.replace(':', '_')))
        root = ET.parse(osc.core.http_GET(url)).getroot()
        for arch in [n.attrib['name'] for n in root.findall('entry')]:
            query = {'nosource': 1}
            url = osc.core.makeurl(
                self.apiurl,
                ('build', a.src_project, a.tgt_project.replace(':', '_'), arch, a.src_package),
                query=query)

            root = ET.parse(osc.core.http_GET(url)).getroot()

            for binary in root.findall('binary'):
                m = pkgname_re.match(binary.attrib['filename'])
                if m:
                    # can't use arch here as the patchinfo mixes all
                    # archs
                    packages.append(Package(m.group('name'), m.group('version'), m.group('release')))
                elif binary.attrib['filename'] == 'updateinfo.xml':
                    url = osc.core.makeurl(
                        self.apiurl,
                        ('build', a.src_project, a.tgt_project.replace(':', '_'),
                         arch,
                         a.src_package,
                         'updateinfo.xml'))
                    ui = ET.parse(osc.core.http_GET(url)).getroot()
                    patch_id = ui.find('.//id').text

        if not packages:
            raise Exception("no packages found")

        self.logger.debug('found packages %s and patch id %s', ' '.join(set([p.name for p in packages])), patch_id)

        for update in PROJECT_OPENQA_SETTINGS[a.tgt_project]:
            settings = update.settings(a.src_project, a.tgt_project, packages, req)
            settings['INCIDENT_PATCH'] = patch_id
            if settings:
                # is old style kgraft check if all options correctly set
                if settings['FLAVOR'] == 'KGraft' and 'VIRSH_GUESTNAME' not in settings:
                    self.logger.info("build: {!s} hasn't valid values for kgraft".format(settings['BUILD']))
                    return None

                # don't start KGRAFT job on Server-DVD-Incidents FLAVOR
                if settings['FLAVOR'] == 'Server-DVD-Incidents':
                    if settings['BUILD'].split('.')[1].startswith('kgraft-patch'):
                        return None

                # kernel incidents jobs -- discard all without 'start' = True
                if settings['FLAVOR'] == 'Server-DVD-Incidents-Kernel':
                    if 'start' in settings:
                        del settings['start']
                    else:
                        return None

                update.calculate_lastest_good_updates(self.openqa, settings)

                self.logger.info("posting %s %s %s", settings['VERSION'], settings['ARCH'], settings['BUILD'])
                self.logger.debug('\n'.join(["  %s=%s" % i for i in settings.items()]))
                if not self.dryrun:
                    try:
                        ret = self.openqa.openqa_request('POST', 'isos', data=settings, retries=1)
                        self.logger.info(pformat(ret))
                    except JSONDecodeError as e:
                        self.logger.error(e)
                        # TODO: record error
                    except openqa_exceptions.RequestError as e:
                        self.logger.error(e)

        return None

    # check a set of repos for their primary checksums
    @staticmethod
    def calculate_repo_hash(repos):
        m = md5.new()
        # if you want to force it, increase this number
        m.update('b')
        for url in repos:
            url += '/repodata/repomd.xml'
            root = ET.parse(osc.core.http_GET(url)).getroot()
            cs = root.find(
                './/{http://linux.duke.edu/metadata/repo}data[@type="primary"]/{http://linux.duke.edu/metadata/repo}checksum')
            m.update(cs.text)
        return m.hexdigest()

    def is_incident_in_testing(self, incident):
        # hard coded for now as we only run this code for SUSE Maintenance workflow
        project = 'SUSE:Maintenance:%s' % incident

        xpath = "(state/@name='review') and (action/source/@project='%s' and action/@type='maintenance_release')" % (project)
        res = osc.core.search(self.apiurl, request=xpath)['request']
        # return the one and only (or None)
        return res.find('request')

    def calculate_incidents(self, incidents):
        """
        get incident numbers from SUSE:Maintenance:Test project
        returns dict with openQA var name : string with numbers
        """
        l_incidents = []
        for kind, prj in incidents.items():
            packages = osc.core.meta_get_packagelist(self.apiurl, prj)
            incidents = []
            # filter out incidents in staging
            for incident in packages:
                # remove patchinfo. prefix
                incident = incident.replace('_', '.').split('.')[1]
                req = self.is_incident_in_testing(incident)
                # without release request it's in staging
                if req is None:
                    continue

                req_ = osc.core.Request()
                req_.read(req)
                kgraft_target, action = SUSEUpdate.kgraft_target(req_)
                # skip kgraft patches from aggregation
                if kgraft_target:
                    continue
                incidents.append(incident)

            l_incidents.append((kind + '_TEST_ISSUES', ','.join(incidents)))

        return l_incidents

    def jobs_for_target(self, data):
        s = data['settings'][0]
        return self.openqa.openqa_request(
            'GET', 'jobs',
            {
                'distri': s['DISTRI'],
                'version': s['VERSION'],
                'arch': s['ARCH'],
                'flavor': s['FLAVOR'],
                'test': data['test'],
                'latest': '1',
            })['jobs']

    # we don't know the current BUILD and querying all jobs is too expensive
    # so we need to check for one known TEST first
    # if that job doesn't contain the proper hash, we trigger a new one
    # and then we know the build
    def trigger_build_for_target(self, prj, data):
        today = date.today().strftime("%Y%m%d")
        repohash = self.calculate_repo_hash(data['repos'])
        buildnr = None
        j = self.jobs_for_target(data)
        for job in j:
            if job['settings'].get('REPOHASH', '') == repohash:
                # take the last in the row
                buildnr = job['settings']['BUILD']
        self.update_test_builds[prj] = buildnr
        # ignore old build numbers, we want a fresh run every day
        # to find regressions in the tests and to get data about
        # randomly failing tests
        if buildnr and buildnr.startswith(today):
            return

        buildnr = 0

        # not found, then check for the next free build nr
        for job in j:
            build = job['settings']['BUILD']
            if build and build.startswith(today):
                try:
                    nr = int(build.split('-')[1])
                    if nr > buildnr:
                        buildnr = nr
                except BaseException:
                    continue

        buildnr = "%s-%d" % (today, buildnr + 1)

        for s in data['settings']:
            # now schedule it for real
            if 'incidents' in data.keys():
                for x, y in self.calculate_incidents(data['incidents']):
                    s[x] = y
            s['BUILD'] = buildnr
            s['REPOHASH'] = repohash
            self.logger.debug(pformat(s))
            if not self.dryrun:
                try:
                    self.openqa.openqa_request('POST', 'isos', data=s, retries=1)
                except Exception as e:
                    self.logger.debug(e)
        self.update_test_builds[prj] = buildnr

    def check_source_submission(self, src_project, src_package, src_rev, dst_project, dst_package):
        ReviewBot.ReviewBot.check_source_submission(self, src_project, src_package, src_rev, dst_project, dst_package)

    def request_get_openqa_jobs(self, req, incident=True, test_repo=False):
        ret = None
        types = set([a.type for a in req.actions])
        if 'maintenance_release' in types:
            src_prjs = set([a.src_project for a in req.actions])
            if len(src_prjs) != 1:
                raise Exception("can't handle maintenance_release from different incidents")
            build = src_prjs.pop()
            tgt_prjs = set([a.tgt_project for a in req.actions])
            ret = []
            for prj in tgt_prjs:
                if incident and prj in PROJECT_OPENQA_SETTINGS:
                    for u in PROJECT_OPENQA_SETTINGS[prj]:
                        s = u.settings(build, prj, [], req=req)
                        ret += self.openqa.openqa_request(
                            'GET', 'jobs',
                            {
                                'distri': s['DISTRI'],
                                'version': s['VERSION'],
                                'arch': s['ARCH'],
                                'flavor': s['FLAVOR'],
                                'build': s['BUILD'],
                                'scope': 'relevant',
                            })['jobs']
                repo_settings = TARGET_REPO_SETTINGS.get(self.openqa.baseurl, {})
                if test_repo and prj in repo_settings:
                    u = repo_settings[prj]
                    for s in u['settings']:
                        repo_jobs = self.openqa.openqa_request(
                            'GET', 'jobs',
                            {
                                'distri': s['DISTRI'],
                                'version': s['VERSION'],
                                'arch': s['ARCH'],
                                'flavor': s['FLAVOR'],
                                'build':  self.update_test_builds.get(prj, 'UNKNOWN'),
                                'scope': 'relevant',
                            })['jobs']
                        ret += repo_jobs
                        if self.calculate_qa_status(repo_jobs) == QA_INPROGRESS:
                            self.pending_target_repos.add(prj)
        return ret

    def calculate_qa_status(self, jobs=None):
        if not jobs:
            return QA_UNKNOWN

        j = dict()
        has_failed = False
        in_progress = False
        for job in jobs:
            if job['clone_id']:
                continue
            name = job['name']
            if name in j and int(job['id']) < int(j[name]['id']):
                continue
            j[name] = job
            #self.logger.debug('job %s in openQA: %s %s %s %s', job['id'], job['settings']['VERSION'], job['settings']['TEST'], job['state'], job['result'])
            if job['state'] not in ('cancelled', 'done'):
                in_progress = True
            else:
                if job['result'] != 'passed' and job['result'] != 'softfailed':
                    has_failed = True

        if not j:
            return QA_UNKNOWN
        if in_progress:
            return QA_INPROGRESS
        if has_failed:
            return QA_FAILED

        return QA_PASSED

    def check_publish_enabled(self, project):
        url = osc.core.makeurl(self.apiurl, ('source', project, '_meta'))
        root = ET.parse(osc.core.http_GET(url)).getroot()
        node = root.find('publish')
        if node is not None and node.find('disable') is not None:
            return False
        return True

    def add_comment(self, req, msg, state, result=None):
        if not self.do_comments:
            return

        comment = "<!-- openqa state=%s%s -->\n" % (state, ' result=%s' % result if result else '')
        comment += "\n" + msg

        (comment_id, comment_state, comment_result, comment_text) = self.find_obs_request_comment(req, state)

        if comment_id is not None and state == comment_state:
            lines_before = len(comment_text.split('\n'))
            lines_after = len(comment.split('\n'))
            if lines_before == lines_after:
                self.logger.debug("not worth the update, previous comment %s is state %s", comment_id, comment_state)
                return

        self.logger.debug("adding comment to %s, state %s result %s", req.reqid, state, result)
        self.logger.debug("message: %s", msg)
        if not self.dryrun:
            if comment_id is not None:
                self.commentapi.delete(comment_id)
            self.commentapi.add_comment(request_id=req.reqid, comment=str(comment))

    # escape markdown
    @staticmethod
    def emd(str):
        return str.replace('_', '\_')

    def get_step_url(self, testurl, modulename):
        failurl = testurl + '/modules/%s/fails' % modulename
        fails = requests.get(failurl).json()
        failed_step = fails.get('first_failed_step', 1)
        return "[%s](%s#step/%s/%d)" % (self.emd(modulename), testurl, modulename, failed_step)

    def job_test_name(self, job):
        return "%s@%s" % (self.emd(job['settings']['TEST']), self.emd(job['settings']['MACHINE']))

    def summarize_one_openqa_job(self, job):
        testurl = osc.core.makeurl(self.openqa.baseurl, ['tests', str(job['id'])])
        if not job['result'] in ['passed', 'failed', 'softfailed']:
            return '\n- [%s](%s) is %s' % (self.job_test_name(job), testurl, job['result'])

        modstrings = []
        for module in job['modules']:
            if module['result'] != 'failed':
                continue
            modstrings.append(self.get_step_url(testurl, module['name']))

        if len(modstrings):
            return '\n- [%s](%s) failed in %s' % (self.job_test_name(job), testurl, ','.join(modstrings))
        elif job['result'] == 'failed':  # rare case: fail without module fails
            return '\n- [%s](%s) failed' % (self.job_test_name(job), testurl)
        return ''

    def check_one_request(self, req):
        ret = None

        try:
            jobs = self.request_get_openqa_jobs(req)
            qa_state = self.calculate_qa_status(jobs)
            self.logger.debug("request %s state %s", req.reqid, qa_state)
            msg = None
            if self.force or qa_state == QA_UNKNOWN:
                ret = ReviewBot.ReviewBot.check_one_request(self, req)
                jobs = self.request_get_openqa_jobs(req)

                if self.force:
                    # make sure to delete previous comments if we're forcing
                    (comment_id, comment_state, comment_result, comment_text) = self.find_obs_request_comment(req)
                    if comment_id is not None:
                        self.logger.debug("deleting old comment %s", comment_id)
                        if not self.dryrun:
                            self.commentapi.delete(comment_id)

                if not jobs:
                    msg = "no openQA tests defined"
                    self.add_comment(req, msg, 'done', 'accepted')
                    ret = True
                else:
                    # no notification until the result is done
                    osc.core.change_review_state(req.apiurl, req.reqid, newstate='new',
                                                 by_group=self.review_group, by_user=self.review_user,
                                                 message='now testing in openQA')
            elif qa_state == QA_FAILED or qa_state == QA_PASSED:
                # don't take test repo results into the calculation of total
                # this is for humans to decide which incident broke the test repo
                jobs += self.request_get_openqa_jobs(req, incident=False, test_repo=True)
                if self.calculate_qa_status(jobs) == QA_INPROGRESS:
                    self.logger.debug(
                        "incident tests for request %s are done, but need to wait for test repo", req.reqid)
                    return
                groups = dict()
                for job in jobs:
                    gl = "%s@%s" % (self.emd(job['group']), self.emd(job['settings']['FLAVOR']))
                    if gl not in groups:
                        groupurl = osc.core.makeurl(self.openqa.baseurl, ['tests', 'overview'],
                                                    {'version': job['settings']['VERSION'],
                                                     'groupid': job['group_id'],
                                                     'flavor': job['settings']['FLAVOR'],
                                                     'distri': job['settings']['DISTRI'],
                                                     'build': job['settings']['BUILD'],
                                                     })
                        groups[gl] = {'title': "__Group [%s](%s)__\n" % (gl, groupurl),
                                      'passed': 0, 'failed': []}

                    job_summary = self.summarize_one_openqa_job(job)
                    if not len(job_summary):
                        groups[gl]['passed'] = groups[gl]['passed'] + 1
                        continue
                    # if there is something to report, hold the request
                    qa_state = QA_FAILED
                    gmsg = groups[gl]
                    groups[gl]['failed'].append(job_summary)

                if qa_state == QA_PASSED:
                    self.logger.debug("request %s passed", req.reqid)
                    msg = "openQA tests passed\n"
                    state = 'accepted'
                    ret = True
                else:
                    self.logger.debug("request %s failed", req.reqid)
                    msg = "openQA tests problematic\n"
                    state = 'declined'
                    ret = False

                for group in sorted(groups.keys()):
                    msg += "\n\n" + groups[group]['title']
                    msg += "(%d tests passed, %d failed)\n" % (groups[group]['passed'], len(groups[group]['failed']))
                    for fail in groups[group]['failed']:
                        msg += fail

                self.add_comment(req, msg, 'done', state)
            elif qa_state == QA_INPROGRESS:
                self.logger.debug("request %s still in progress", req.reqid)
            else:
                raise Exception("unknown QA state %d", qa_state)

        except Exception:
            import traceback
            self.logger.error("unhandled exception in openQA Bot")
            self.logger.error(traceback.format_exc())
            ret = None

        return ret

    def find_obs_request_comment(self, req, state=None):
        """Return previous comments (should be one)."""
        if self.do_comments:
            comments = self.commentapi.get_comments(request_id=req.reqid)
            for c in comments.values():
                m = comment_marker_re.match(c['comment'])
                if m and (state is None or state == m.group('state')):
                    return c['id'], m.group('state'), m.group('result'), c['comment']
        return None, None, None, None


class CommandLineInterface(ReviewBot.CommandLineInterface):

    def __init__(self, *args, **kwargs):
        ReviewBot.CommandLineInterface.__init__(self, args, kwargs)
        self.clazz = OpenQABot

    def get_optparser(self):
        parser = ReviewBot.CommandLineInterface.get_optparser(self)
        parser.add_option("--force", action="store_true", help="recheck requests that are already considered done")
        parser.add_option("--no-comment", dest='comment', action="store_false",
                          default=True, help="don't actually post comments to obs")
        parser.add_option("--openqa", metavar='HOST', help="openqa api host")
        return parser

    def setup_checker(self):
        bot = ReviewBot.CommandLineInterface.setup_checker(self)

        if self.options.force:
            bot.force = True
        bot.do_comments = self.options.comment
        if not self.options.openqa:
            raise osc.oscerr.WrongArgs("missing openqa url")
        bot.openqa = OpenQA_Client(server=self.options.openqa)

        global logger
        logger = self.logger

        return bot


if __name__ == "__main__":
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.WARNING)
    requests_log.propagate = False

    app = CommandLineInterface()
    sys.exit(app.main())

# vim: sw=4 et
