import os
from unittest.mock import patch
from typing import List
import shutil
from pathlib import Path

import pendulum
import vcr

from academic_observatory_workflows.config import project_path, TestConfig
from academic_observatory_workflows.crossref_fundref_telescope.release import CrossrefFundrefRelease
from academic_observatory_workflows.crossref_fundref_telescope import tasks
from observatory_platform.dataset_api import DatasetAPI
from observatory_platform.date_utils import datetime_normalise
from observatory_platform.google.bigquery import bq_sharded_table_id
from observatory_platform.google.gcs import gcs_blob_name_from_path, gcs_upload_files
from observatory_platform.sandbox.sandbox_environment import SandboxEnvironment
from observatory_platform.sandbox.test_utils import SandboxTestCase
from observatory_platform.files import load_jsonl, get_file_hash

FIXTURES_FOLDER = project_path("crossref_fundref_telescope", "tests", "fixtures")
SCHEMA_FOLDER = project_path("crossref_fundref_telescope", "schema")


class TestTasks(SandboxTestCase):

    def test_list_releases(self):
        """Test that list releases returns a list with dictionaries of release info.

        :return: None.
        """

        cassette_path = os.path.join(FIXTURES_FOLDER, "list_fundref_releases.yaml")
        with vcr.use_cassette(cassette_path):
            releases = tasks.list_releases(pendulum.datetime(2014, 3, 1), pendulum.datetime(2020, 6, 1))
            self.assertIsInstance(releases, List)
            self.assertEqual(39, len(releases))
            for release in releases:
                self.assertIsInstance(release, dict)
                self.assertIsInstance(release["url"], str)
                self.assertIsInstance(pendulum.parse(release["snapshot_date"]), pendulum.DateTime)

    def test_upload_transformed(self):
        """Tests that the upload_transformed function uploads to the GCS transform bucket"""

        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )

        with env.create():
            # Set up file
            Path(release.transform_file_path).touch()
            blob_name = gcs_blob_name_from_path(release.transform_file_path)

            # Run the upload function
            tasks.upload_transformed(release.to_dict())
            self.assert_blob_exists(env.transform_bucket, blob_name)
            self.assert_blob_integrity(env.transform_bucket, blob_name, release.transform_file_path)

    def test_cleanup_workflow(self):
        """Test the cleanup_workflow function"""

        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        with env.create():
            release = CrossrefFundrefRelease(
                cloud_workspace=env.cloud_workspace,
                snapshot_date=pendulum.datetime(2024, 1, 1),
                dag_id="crossref_fundref",
                run_id="run_id",
                url="",
                data_interval_start=pendulum.datetime(2024, 1, 1),
                data_interval_end=pendulum.datetime(2024, 1, 1).end_of("month"),
            )
            # Create the folders
            workflow_folder = release.workflow_folder
            release.download_folder
            release.transform_folder
            release.extract_folder

            # Run cleanup and make sure the folders are gone
            tasks.cleanup_workflow(release.to_dict())
            self.assert_cleanup(workflow_folder)

    def test_download(self):
        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="https://gitlab.com/api/v4/projects/crossref%2Fopen_funder_registry/releases?per_page=100&page=1",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )
        download_cassette = os.path.join(FIXTURES_FOLDER, "fundref_e2e.yaml")
        with env.create():
            with vcr.use_cassette(download_cassette):
                tasks.download(release.to_dict())
            self.assertTrue(os.path.exists(release.download_file_path))

    def test_upload_downloaded(self):
        """Tests that the upload_downloaded function uploads to the GCS download bucket"""

        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )
        download_path = os.path.join(FIXTURES_FOLDER, "crossref_fundref_v1.34.tar.gz")
        with env.create():
            shutil.copy(download_path, release.download_file_path)
            tasks.upload_downloaded(release.to_dict())
            blob_name = gcs_blob_name_from_path(release.download_file_path)
            self.assert_blob_exists(env.download_bucket, blob_name)
            self.assert_blob_integrity(env.download_bucket, blob_name, release.download_file_path)

    def test_extract(self):
        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )
        with env.create():
            download_path = os.path.join(FIXTURES_FOLDER, "crossref_fundref_v1.34.tar.gz")
            extract_path = os.path.join(FIXTURES_FOLDER, "crossref_fundref_extracted.rdf")
            shutil.copy(download_path, release.download_file_path)
            tasks.extract(release.to_dict())
            self.assertTrue(os.path.exists(release.extract_file_path))
            self.assertEqual(get_file_hash(file_path=extract_path), get_file_hash(file_path=release.extract_file_path))

    def test_transform(self):
        """Tests the transform function"""
        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )

        input_data_file = os.path.join(FIXTURES_FOLDER, "crossref_fundref_extracted.rdf")
        expected_output_data_file = os.path.join(FIXTURES_FOLDER, "crossref_fundref_transformed.jsonl")
        with env.create():
            shutil.copy(input_data_file, release.extract_file_path)
            tasks.transform(release.to_dict())
            self.assertTrue(os.path.exists(release.transform_file_path))
            actual_output = load_jsonl(release.transform_file_path)
            expected_output = load_jsonl(expected_output_data_file)
            self.assertEqual(actual_output, expected_output)

    def test_bq_load(self):
        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        dataset_id = env.add_dataset(prefix="crossref_fundref_test")

        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=pendulum.datetime(2024, 1, 1),
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=pendulum.now(),
            data_interval_end=pendulum.now(),
        )
        transformed_data_file = os.path.join(FIXTURES_FOLDER, "single_item_transformed.jsonl")
        with env.create():
            # upload the transformed files to the bucket
            shutil.copy(transformed_data_file, release.transform_file_path)
            success = gcs_upload_files(
                bucket_name=release.cloud_workspace.transform_bucket, file_paths=[release.transform_file_path]
            )
            self.assertTrue(success)

            tasks.bq_load(
                release.to_dict(),
                bq_dataset_id=dataset_id,
                bq_table_name="crossref_fundref",
                dataset_description="",
                table_description="",
                schema_folder=SCHEMA_FOLDER,
            )
            table_id = bq_sharded_table_id(
                release.cloud_workspace.output_project_id, dataset_id, "crossref_fundref", release.snapshot_date
            )
            self.assert_table_integrity(table_id, expected_rows=1)

    def test_add_dataset_release(self):
        env = SandboxEnvironment(project_id=TestConfig.gcp_project_id, data_location=TestConfig.gcp_data_location)
        api_dataset_id = env.add_dataset(prefix="crossref_fundref_test_api")
        now = pendulum.now("UTC")

        snapshot_date = pendulum.datetime(2024, 1, 1)
        release = CrossrefFundrefRelease(
            cloud_workspace=env.cloud_workspace,
            snapshot_date=snapshot_date,
            dag_id="crossref_fundref",
            run_id="run_id",
            url="",
            data_interval_start=snapshot_date,
            data_interval_end=snapshot_date.end_of("month"),
        )
        with env.create():
            expected_api_release = {
                "dag_id": "crossref_fundref",
                "entity_id": "crossref_fundref",
                "dag_run_id": "run_id",
                "created": now.to_iso8601_string(),
                "modified": now.to_iso8601_string(),
                "data_interval_start": "2024-01-01T00:00:00Z",
                "data_interval_end": "2024-01-31T23:59:59Z",
                "snapshot_date": "2024-01-01T00:00:00Z",
                "partition_date": None,
                "changefile_start_date": None,
                "changefile_end_date": None,
                "sequence_start": None,
                "sequence_end": None,
                "extra": {},
            }
            api = DatasetAPI(bq_project_id=release.cloud_workspace.project_id, bq_dataset_id=api_dataset_id)

            # Should not be any releases in the API before the task is run
            self.assertEqual(len(api.get_dataset_releases(dag_id=release.dag_id, entity_id="crossref_fundref")), 0)
            with patch("academic_observatory_workflows.crossref_fundref_telescope.tasks.pendulum.now") as mock_now:
                mock_now.return_value = now
                tasks.add_dataset_releases(release.to_dict(), api_bq_dataset_id=api_dataset_id)

            # Should be one release in the API
            api_releases = api.get_dataset_releases(dag_id=release.dag_id, entity_id="crossref_fundref")
        self.assertEqual(len(api_releases), 1)
        self.assertEqual(expected_api_release, api_releases[0].to_dict())
