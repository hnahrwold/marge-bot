import contextlib
from unittest.mock import Mock, patch

import pytest

import marge.commit
import marge.bot
import marge.git
import marge.gitlab
import marge.job
import marge.project
import marge.user
from marge.approvals import Approvals
from marge.gitlab import GET, PUT
from marge.merge_request import MergeRequest

import tests.test_approvals as test_approvals
import tests.test_commit as test_commit
import tests.test_merge_request as test_merge_request
import tests.test_project as test_project
import tests.test_user as test_user
from tests.gitlab_api_mock import Api as ApiMock, Error, Ok


class struct:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

def _commit(id, status):
    return  {
        'id': id,
        'short_id': id,
        'author_name': 'J. Bond',
        'author_email': 'jbond@mi6.gov.uk',
        'message': 'Shaken, not stirred',
        'status': status,
    }

class MockLab(object):
    def __init__(self, gitlab_url=None):
        self.gitlab_url = gitlab_url = gitlab_url or 'http://git.example.com'
        self.api = api = ApiMock(gitlab_url=gitlab_url, auth_token='no-token', initial_state='initial')

        api.add_transition(GET('/version'), Ok({'version': '9.2.3-ee'}))

        self.user_info = dict(test_user.INFO)
        self.user_id = self.user_info['id']
        api.add_user(self.user_info, is_current=True)

        self.project_info = dict(test_project.INFO)
        api.add_project(self.project_info)

        self.commit_info = dict(test_commit.INFO)
        api.add_commit(self.project_info['id'], self.commit_info)

        self.author_id = 234234
        self.merge_request_info = {
            'id':  53,
            'iid': 54,
            'title': 'a title',
            'project_id': 1234,
            'author': {'id': self.author_id},
            'assignee': {'id': self.user_id},
            'approved_by': [],
            'state': 'opened',
            'sha': self.commit_info['id'],
            'source_project_id': 1234,
            'target_project_id': 1234,
            'source_branch': 'useless_new_feature',
            'target_branch': 'master',
            'work_in_progress': False,
            'web_url': 'http://git.example.com/group/project/merge_request/666',
        }
        api.add_merge_request(self.merge_request_info)

        self.initial_master_sha = '505e'
        self.rewritten_sha = rewritten_sha = 'af7a'
        commit_after_pushing = _commit(id=rewritten_sha, status='running')
        api.add_transition(
           GET('/projects/1234/repository/commits/%s' % rewritten_sha),
           Ok(commit_after_pushing),
           from_state='pushed', to_state='passed',
        )
        api.add_transition(
           GET('/projects/1234/repository/commits/%s' % rewritten_sha),
           Ok(_commit(id=rewritten_sha, status='success')),
           from_state=['passed', 'merged'],
        )
        api.add_transition(
           GET('/projects/1234/repository/branches/useless_new_feature'),
           Ok({'commit': _commit(id=rewritten_sha, status='success')}),
           from_state='passed'
        )
        api.add_transition(
            PUT(
                '/projects/1234/merge_requests/54/merge',
                dict(sha=rewritten_sha, should_remove_source_branch=True, merge_when_pipeline_succeeds=True),
            ),
            Ok({}),
            from_state='passed', to_state='merged',
        )
        api.add_merge_request(dict(self.merge_request_info, state='merged'), from_state='merged')
        self.approvals_info = dict(
            test_approvals.INFO,
            id=self.merge_request_info['id'],
            iid=self.merge_request_info['iid'],
            project_id=self.merge_request_info['project_id'],
            approvals_left=0,
        )
        api.add_approvals(self.approvals_info)
        api.add_transition(
            GET('/projects/1234/repository/branches/master'),
            Ok({'commit': {'id': self.initial_master_sha}}),
        )
        api.add_transition(
            GET('/projects/1234/repository/branches/master'),
            Ok({'commit': {'id': self.rewritten_sha}}),
            from_state='merged'
        )
        api.expected_note(
            self.merge_request_info,
            "My job would be easier if people didn't jump the queue and pushed directly... *sigh*",
            from_state=['pushed_but_master_moved', 'merge_rejected'],
        )
        api.expected_note(
            self.merge_request_info,
            "I'm broken on the inside, please somebody fix me... :cry:"
        )

    def push_rebased(self, *args, **kwargs):
        self.api.state = 'pushed'
        rebased_sha = 'deadbeef'
        return self.initial_master_sha, rebased_sha, self.rewritten_sha

    @contextlib.contextmanager
    def expected_failure(self, message):
        author_assigned = False
        def assign_to_author():
            nonlocal author_assigned
            author_assigned = True

        self.api.add_transition(
            PUT('/projects/1234/merge_requests/54', args={'assignee_id': self.author_id}),
            assign_to_author,
        )
        error_note = "I couldn't merge this branch: %s" % message
        self.api.expected_note(self.merge_request_info, error_note)

        yield

        assert author_assigned
        assert error_note in self.api.notes

@patch('time.sleep')
class TestRebaseAndAccept(object):
    def setup_method(self, _method):
        self.mocklab = MockLab()
        self.api = self.mocklab.api

    def make_job(self, options=None):
        api, mocklab = self.api, self.mocklab

        project_id = mocklab.project_info['id']
        merge_request_iid = mocklab.merge_request_info['iid']

        project = marge.project.Project.fetch_by_id(project_id, api)
        merge_request = MergeRequest.fetch_by_iid(project_id, merge_request_iid, api)

        repo = Mock(marge.git.Repo)
        options = options or marge.job.MergeJobOptions.default()
        user = marge.user.User.myself(self.api)
        bot = marge.bot.Bot(
            api=self.api,
            user=user,
            ssh_key_file='id_rsa',
            add_reviewers=options.add_reviewers,
            add_tested=options.add_tested,
            impersonate_approvers=options.reapprove,
        )
        return marge.job.MergeJob(bot=bot, project=project, merge_request=merge_request, repo=repo)

    def test_succeeds_first_time(self, time_sleep):
        api, mocklab = self.api, self.mocklab
        with patch('marge.job.push_rebased_and_rewritten_version', side_effect=mocklab.push_rebased):
            job = self.make_job(marge.job.MergeJobOptions.default(add_tested=True, add_reviewers=False))
            job.execute()

        assert api.state == 'merged'
        assert api.notes == []

    def test_fails_on_not_acceptable_if_master_did_not_move(self, time_sleep):
        api, mocklab = self.api, self.mocklab
        new_branch_head_sha = '99ba110035'
        api.add_transition(
           GET('/projects/1234/repository/branches/useless_new_feature'),
           Ok({'commit': _commit(id=new_branch_head_sha, status='success')}),
           from_state='passed', to_state='passed_but_head_changed'
        )
        with patch('marge.job.push_rebased_and_rewritten_version', side_effect=mocklab.push_rebased):
            with mocklab.expected_failure("Someone pushed to branch while we were trying to merge"):
                job = self.make_job(marge.job.MergeJobOptions.default(add_tested=True, add_reviewers=False))
                job.execute()

        assert api.state == 'passed_but_head_changed'
        assert api.notes == ["I couldn't merge this branch: Someone pushed to branch while we were trying to merge"]

    def test_succeeds_second_time_if_master_moved(self, time_sleep):
        api, mocklab = self.api, self.mocklab
        moved_master_sha = 'fafafa'
        first_rewritten_sha = '1o1'
        api.add_transition(
           GET('/projects/1234/repository/commits/%s' % first_rewritten_sha),
           Ok(_commit(id=first_rewritten_sha, status='success')),
           from_state=['pushed_but_master_moved', 'merged_rejected'],
        )
        api.add_transition(
            PUT(
                '/projects/1234/merge_requests/54/merge',
                dict(sha=first_rewritten_sha, should_remove_source_branch=True, merge_when_pipeline_succeeds=True),
            ),
            Error(marge.gitlab.NotAcceptable()),
            from_state='pushed_but_master_moved', to_state='merge_rejected',
        )
        api.add_transition(
           GET('/projects/1234/repository/branches/useless_new_feature'),
           Ok({'commit': _commit(id=first_rewritten_sha, status='success')}),
           from_state='pushed_but_master_moved'
        )
        api.add_transition(
            GET('/projects/1234/repository/branches/master'),
            Ok({'commit': _commit(id=moved_master_sha, status='success')}),
            from_state='merge_rejected'
        )

        def push_effects():
            assert api.state == 'initial'
            api.state = 'pushed_but_master_moved'
            yield mocklab.initial_master_sha, 'f00ba4', first_rewritten_sha

            assert api.state == 'merge_rejected'
            api.state = 'pushed'
            yield moved_master_sha, 'deadbeef', mocklab.rewritten_sha

        with patch('marge.job.push_rebased_and_rewritten_version', side_effect=push_effects()):
            job = self.make_job(marge.job.MergeJobOptions.default(add_tested=True, add_reviewers=False))
            job.execute()

        assert api.state == 'merged'
        assert api.notes == ["My job would be easier if people didn't jump the queue and pushed directly... *sigh*"]

    def test_wont_merge_wip_stuff(self, time_sleep):
        api, mocklab = self.api, self.mocklab
        wip_merge_request = dict(mocklab.merge_request_info, work_in_progress=True)
        api.add_merge_request(wip_merge_request, from_state='initial')

        with mocklab.expected_failure("Sorry, I can't merge requests marked as Work-In-Progress!"):
            job = self.make_job()
            job.execute()

        assert api.state == 'initial'
        assert api.notes == ["I couldn't merge this branch: Sorry, I can't merge requests marked as Work-In-Progress!"]
