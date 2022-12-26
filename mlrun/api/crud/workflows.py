# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import uuid
from typing import List

from sqlalchemy.orm import Session

import mlrun.api.schemas
import mlrun.utils.singleton
from mlrun.api.api.utils import (
    apply_enrichment_and_validation_on_function,
    get_run_db_instance,
    get_scheduler,
)
from mlrun.config import config


class WorkflowRunners(
    metaclass=mlrun.utils.singleton.Singleton,
):
    def create_workflow_runner(
        self,
        run_name: str,
        db_session: Session,
        auth_info: mlrun.api.schemas.AuthInfo,
        project: str = None,
    ):
        workflow_runner = mlrun.new_function(
            name=run_name,
            project=project,
            kind=mlrun.runtimes.RuntimeKinds.job,
            # For preventing deployment
            image=mlrun.mlconf.default_base_image,
        )

        workflow_runner.set_db_connection(get_run_db_instance(db_session))

        # Enrichment and validation requires access key
        workflow_runner.metadata.credentials.access_key = (
            mlrun.model.Credentials.generate_access_key
        )

        apply_enrichment_and_validation_on_function(
            function=workflow_runner,
            auth_info=auth_info,
        )

        workflow_runner.save()
        return workflow_runner

    def schedule_workflow_runner(
        self,
        function: mlrun.runtimes.BaseRuntime,
        project: mlrun.api.schemas.Project,
        workflow_spec: mlrun.api.schemas.WorkflowSpec,
        artifact_path: str,
        namespace: str,
        db_session: Session = None,
        auth_info: mlrun.api.schemas.AuthInfo = None,
    ):
        labels = [("job-type", "workflow-runner"), ("workflow", workflow_spec.name)]

        run_spec = _prepare_run_object_for_scheduling(
            project=project,
            workflow_spec=workflow_spec,
            run_name=function.metadata.name,
            artifact_path=artifact_path,
            namespace=namespace,
            labels=labels,
        )

        # Creating scheduled object:
        scheduled_object = {
            "task": run_spec.to_dict(),
            "schedule": workflow_spec.schedule,
        }

        # Creating schedule:
        get_scheduler().create_schedule(
            db_session=db_session,
            auth_info=auth_info,
            project=project.metadata.name,
            name=function.metadata.name,
            kind=mlrun.api.schemas.ScheduleKinds.job,
            scheduled_object=scheduled_object,
            cron_trigger=workflow_spec.schedule,
            labels=function.metadata.labels,
        )

    def execute_workflow_runner(
        self,
        function: mlrun.runtimes.BaseRuntime,
        project: mlrun.api.schemas.Project,
        workflow_spec: mlrun.api.schemas.WorkflowSpec = None,
        artifact_path: str = None,
        namespace: str = None,
        load_only: bool = False,
    ):
        if load_only:
            labels = [
                ("job-type", "project-loader"),
                ("project", project.metadata.name),
            ]
        else:
            run_name = function.metadata.name or workflow_spec.name
            labels = [("job-type", "workflow-runner"), ("workflow", run_name)]

        runspec = _prepare_run_object_for_single_run(
            project=project,
            workflow_spec=workflow_spec,
            run_name=function.metadata.name,
            artifact_path=artifact_path,
            namespace=namespace,
            labels=labels,
            load_only=load_only,
        )

        return function.run(
            runspec=runspec, artifact_path=artifact_path or "", local=False
        )


def _label_run_object(
    run_object: mlrun.run.RunObject, labels: List
) -> mlrun.run.RunObject:
    for key, value in labels:
        run_object = run_object.set_label(key, value)
    return run_object


def _prepare_run_object_for_scheduling(
    project,
    workflow_spec,
    run_name,
    artifact_path,
    namespace,
    labels: List,
) -> mlrun.run.RunObject:
    meta_uid = uuid.uuid4().hex

    run_spec = {
        "spec": {
            "parameters": {
                "url": project.spec.source,
                "project_name": project.metadata.name,
                "workflow_name": workflow_spec.name,
                "workflow_path": workflow_spec.path,
                "workflow_arguments": workflow_spec.args,
                "artifact_path": artifact_path,
                "workflow_handler": workflow_spec.handler,
                "namespace": namespace,
                "ttl": workflow_spec.ttl,
                "engine": workflow_spec.engine,
                "local": workflow_spec.run_local,
            },
            "handler": "mlrun.projects.load_and_run",
            "scrape_metrics": config.scrape_metrics,
            "output_path": (artifact_path or config.artifact_path).replace(
                "{{run.uid}}", meta_uid
            ),
        },
        "metadata": {
            "uid": meta_uid,
            "project": project.metadata.name,
            "name": workflow_spec.name,
        },
    }

    # Creating object:
    run_object = mlrun.RunObject.from_dict(run_spec)

    # Setting labels:
    return _label_run_object(run_object, labels)


def _prepare_run_object_for_single_run(
    project: mlrun.api.schemas.Project,
    labels: List,
    workflow_spec: mlrun.api.schemas.WorkflowSpec = None,
    run_name: str = None,
    artifact_path: str = None,
    namespace: str = None,
    load_only: bool = False,
) -> mlrun.run.RunObject:
    run_spec = {
        "spec": {
            "parameters": {
                "url": project.spec.source,
                "project_name": project.metadata.name,
                "load_only": load_only,
            },
            "handler": "mlrun.projects.load_and_run",
        },
        "metadata": {"name": run_name},
    }
    if not load_only:
        run_spec["spec"]["parameters"].update(
            {
                "workflow_name": run_name,
                "workflow_path": workflow_spec.path,
                "workflow_arguments": workflow_spec.args,
                "artifact_path": artifact_path,
                "workflow_handler": workflow_spec.handler,
                "namespace": namespace,
                "ttl": workflow_spec.ttl,
                "engine": workflow_spec.engine,
                "local": workflow_spec.run_local,
            }
        )

    # Creating object:
    run_object = mlrun.RunObject.from_dict(run_spec)

    # Setting labels:
    return _label_run_object(run_object, labels)
