"""Unfinished production work follows an explicit payer change without moving ownership."""

from backend.db import Task
from backend.model_access import access_scope
from backend.production_nodes import adopt_current_payer


def test_unsubmitted_task_adopts_the_current_payer():
    task = Task(id='waiting-node', kind='image', status='waiting', session_id='account-wallet', result={})
    with access_scope('own-keys'):
        assert adopt_current_payer(task) is True
    assert task.session_id == 'own-keys'


def test_submitted_provider_job_keeps_its_original_payer():
    task = Task(id='submitted-node', kind='video', status='waiting', session_id='account-wallet',
                result={'provider_id': 'provider-job'})
    with access_scope('own-keys'):
        assert adopt_current_payer(task) is False
    assert task.session_id == 'account-wallet'
