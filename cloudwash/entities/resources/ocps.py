import tempfile

from cloudwash.config import settings
from cloudwash.constants import CLUSTER_EXP_DATE_TAG
from cloudwash.constants import CLUSTER_ID_TAGS
from cloudwash.constants import CLUSTER_NAME_TAGS
from cloudwash.constants import OCP_TAG_SUBSTR
from cloudwash.entities.resources.base import OCPsCleanup
from cloudwash.logger import logger
from cloudwash.utils import are_resources_older_than
from cloudwash.utils import check_installer_exists
from cloudwash.utils import destroy_ocp_cluster
from cloudwash.utils import dry_data
from cloudwash.utils import write_metadata_file


class LeftoverAWSOcp:
    """Represents an OCP cluster with its collected metadata and filtered AWS resources."""

    def __init__(self, infra_id: str, region: str):
        self.infra_id = infra_id
        self.region = region
        self.associated_resources = {"Resources": [], "Instances": []}
        self._cluster_name = ""
        self._cluster_id = ""
        self._expiration_date = ""

    def __repr__(self):
        instances = len(self.associated_resources.get("Instances", []))
        resources = len(self.associated_resources.get("Resources", []))
        return (
            f"LeftoverAWSOcp({self.infra_id}, region={self.region}, "
            f"instances={instances}, resources={resources})"
        )

    def _collect_all_tags(self) -> dict:
        """Build a merged tag dict from all associated resources (first value wins)."""
        tags = {}
        for resources_list in self.associated_resources.values():
            for resource in resources_list:
                for tag in resource.get_tags():
                    key = tag.get("Key", "")
                    if key and key not in tags:
                        tags[key] = tag.get("Value", "")
        return tags

    def get_cluster_info(self):
        """Extract cluster name, ID, and expiration date from resource tags."""
        tags = self._collect_all_tags()
        if not self._expiration_date:
            self._expiration_date = tags.get(CLUSTER_EXP_DATE_TAG, "")
        if not self._cluster_name:
            for name_tag in CLUSTER_NAME_TAGS:
                if name_tag in tags:
                    self._cluster_name = tags[name_tag]
                    break
        if not self._cluster_id:
            for id_tag in CLUSTER_ID_TAGS:
                if id_tag in tags:
                    self._cluster_id = tags[id_tag]
                    break

    def get_cluster_metadata(self) -> dict:
        """Build the metadata dict required by openshift-install for cluster teardown."""
        infra_id = self.infra_id
        cluster_name = self._cluster_name or infra_id
        cluster_id = self._cluster_id or infra_id

        logger.info(f"\nPreparing metadata for cluster: {infra_id}")
        return {
            "clusterName": cluster_name,
            "clusterID": cluster_id,
            "infraID": infra_id,
            "aws": {
                "region": self.region,
                "identifier": [{f"{OCP_TAG_SUBSTR}{infra_id}": "owned"}],
            },
        }


class CleanOCPs(OCPsCleanup):
    def __init__(self):
        self._delete = {"resources": {}, "clusters": []}
        self._cluster_map = {}
        self.list()

    def _set_dry(self):
        dry_data['OCPS']['delete'] = self._delete["resources"]
        dry_data['OCPS']['clusters'] = self._delete["clusters"]

    def list(self):
        pass

    def remove(self):
        pass

    def cleanup(self):
        if not settings.dry_run:
            check_installer_exists()
            with tempfile.TemporaryDirectory() as tmpdir:
                for cluster_name in self._delete["clusters"]:
                    cluster = self._cluster_map[cluster_name]
                    cluster.get_cluster_info()
                    metadata = cluster.get_cluster_metadata()
                    metadata_path = write_metadata_file(
                        cluster_metadata=metadata, cleanup_dir=tmpdir
                    )
                    destroy_ocp_cluster(
                        metadata_path=metadata_path,
                        cluster_name=cluster_name,
                    )


class CleanAWSOcps(CleanOCPs):
    def __init__(self, client):
        self.client = client
        self.cleaning_region = self.client.cleaning_region
        super().__init__()

    def group_ocps_by_cluster(self, resources: list = None) -> dict:
        """Group AWS resources under their originating OCP clusters by infra ID.

        :param list resources: AWS resources collected by region and SLA
        :return: Dict mapping cluster infra IDs to LeftoverAWSOcp instances
        """
        if resources is None:
            resources = []
        clusters_map = {}

        for resource in resources:
            for key in resource.get_tags(regex=OCP_TAG_SUBSTR):
                cluster_infra_id = key.get("Key")
                if OCP_TAG_SUBSTR in cluster_infra_id:
                    parts = cluster_infra_id.split(OCP_TAG_SUBSTR)
                    if len(parts) < 2 or not parts[1]:
                        continue
                    cluster_infra_id = parts[1]
                    if cluster_infra_id not in clusters_map:
                        clusters_map[cluster_infra_id] = LeftoverAWSOcp(
                            infra_id=cluster_infra_id, region=self.cleaning_region
                        )

                    if hasattr(resource, 'ec2_instance'):
                        clusters_map[cluster_infra_id].associated_resources["Instances"].append(
                            resource
                        )
                    else:
                        clusters_map[cluster_infra_id].associated_resources["Resources"].append(
                            resource
                        )
        return clusters_map

    def _filter_deletable(self):
        """Apply SLA and exception filters to determine which clusters are deletable."""
        sla = settings.aws.criteria.ocps.get("SLA")
        exceptions = list(settings.aws.exceptions.get("OCPS", []))

        for cluster_name, cluster in self._cluster_map.items():
            if cluster_name in exceptions:
                continue

            instances = cluster.associated_resources.get("Instances", [])
            resources = cluster.associated_resources.get("Resources", [])

            if instances:
                if are_resources_older_than(time_ref=sla, resources=instances):
                    self._delete["clusters"].append(cluster_name)
                    self._delete["resources"].update(self._resources_by_type(resources + instances))
            elif resources:
                if are_resources_older_than(time_ref=sla, resources=resources):
                    self._delete["clusters"].append(cluster_name)
                    self._delete["resources"].update(self._resources_by_type(resources))

    @staticmethod
    def _resources_by_type(resources: list) -> dict:
        """Group resource names by their resource_type."""
        result = {}
        for r in resources:
            result.setdefault(r.resource_type, []).append(r.name)
        return result

    def list(self):
        resources = []
        ocp_prefixes = list(settings.aws.criteria.ocps.get("OCP_PREFIXES") or [""])
        for prefix in ocp_prefixes:
            query = " ".join(
                [f"tag.key:{OCP_TAG_SUBSTR}{prefix}*", f"region:{self.cleaning_region}"]
            )
            resources.extend(self.client.list_resources(query=query))

        self._cluster_map = self.group_ocps_by_cluster(resources=resources)
        self._filter_deletable()

        self._delete["clusters"] = sorted(self._delete["clusters"])
        self._set_dry()
