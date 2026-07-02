"""Common utils for cleanup activities of all CRs"""
import importlib.resources
import json
import subprocess
from collections import namedtuple
from datetime import datetime
from pathlib import Path

import dateparser
import dominate
import pytz
from dominate.tags import caption
from dominate.tags import div
from dominate.tags import h1
from dominate.tags import style
from dominate.tags import table
from dominate.tags import tbody
from dominate.tags import td
from dominate.tags import tr
from dominate.util import raw
from wrapanapi.systems.ec2 import ResourceExplorerResource

from cloudwash.assets import css
from cloudwash.logger import logger

_vms_dict = {"VMS": {"delete": [], "stop": [], "skip": []}}
_containers_dict = {"CONTAINERS": {"delete": [], "stop": [], "skip": []}}

dry_data = {
    "NICS": {"delete": []},
    "DISCS": {"delete": []},
    "PIPS": {"delete": []},
    "OCPS": {"delete": [], "clusters": []},
    "RESOURCES": {"delete": []},
    "STACKS": {"delete": []},
    "IMAGES": {"delete": []},
    "PROVIDER": "",
    "REGION": "",
    "GROUP": "",
    "ZONE": "",
}
non_rt_keys = ('provider', 'zone', 'region', 'group')

dry_data.update(_vms_dict)
dry_data.update(_containers_dict)


def resourcewise_data(dry_data=None) -> dict:
    resource_data = {
        "provider": dry_data.get('PROVIDER'),
        "region": dry_data.get('REGION'),
        "group": dry_data.get('GROUP'),
        "zone": dry_data.get('ZONE'),
        "deletable_vms": dry_data["VMS"]["delete"],
        "stopable_vms": dry_data["VMS"]["stop"],
        "skipped_vms": dry_data["VMS"]["skip"],
        "deletable_containers": dry_data["CONTAINERS"]["delete"],
        "stopable_containers": dry_data["CONTAINERS"]["stop"],
        "skipped_containers": dry_data["CONTAINERS"]["skip"],
        "deletable_discs": dry_data["DISCS"]["delete"],
        "deletable_nics": dry_data["NICS"]["delete"],
        "deletable_images": dry_data["IMAGES"]["delete"],
        "deletable_pips": dry_data["PIPS"]["delete"] if "PIPS" in dry_data else None,
        "deletable_resources": dry_data["RESOURCES"]["delete"],
        "deletable_stacks": dry_data["STACKS"]["delete"] if "STACKS" in dry_data else None,
        "deletable_ocps": dry_data["OCPS"]["delete"],
    }
    return resource_data


def echo_dry(dry_data=None) -> None:
    """Prints and Logs the per resource cleanup data on STDOUT and logfile

    :param dict dry_data: The deletable resources dry data of a Compute Resource,
        it follows the format of module scoped `dry_data` variable in this module
    """
    logger.info("\n=========== DRY SUMMARY ============\n")

    # Group the same resource type under the same section for logging
    grouped_resources = {}
    resource_data = resourcewise_data(dry_data)
    for key, value in resource_data.items():
        if key not in non_rt_keys and value:
            suffix = key.split('_')[1].upper()
            action = key.split('_')[0].title()

            if suffix not in grouped_resources.keys():
                grouped_resources[suffix] = {}
            grouped_resources[suffix][action] = value

    if any(value for key, value in resource_data.items() if key not in non_rt_keys):
        for suffix, actions in grouped_resources.items():
            logger.info(f"{suffix}:")
            for action, value in actions.items():
                logger.info(f"\t{action}: {value}")
    else:
        logger.info("\nNo resources are eligible for cleanup!\n")

    logger.info("\n====================================\n")


def table_caption(kwargs):
    '''Returns the caption for the table based on the provider'''
    caption_dict = {
        'GCE': f"Zone: {kwargs.get('zone')}",
        'AZURE': f"Region: {kwargs.get('region')}, Group: {kwargs.get('group')}",
        'AWS': f"Region: {kwargs.get('region')}",
    }
    provider = kwargs.get('provider')
    return caption_dict.get(provider, '')


def create_html(provider, all_data):
    """
    Creates an HTML report file with deletable resources for a given provider.
    Args:
        provider (str): The name of the cloud provider.
        all_data (list): A list of dictionaries containing resource data for different regions.
    Returns:
        None: The function writes the generated HTML content to
        a file named 'cleanup_resource_<provider>.html'.
    The generated HTML report includes:
        - A title "Cloud resources page".
        - A header "CLOUDWASH REPORT-<provider>".
        - A table for each region's resource data.
        - Resource names listed with bullet points.
        - Nested resource types and their respective resources.
    Note:
        The function uses the 'dominate' library to create the HTML structure and
        'importlib.resources' to read a CSS file for styling.
    """
    doc = dominate.document(title="Cloud resources page")
    with doc.head:
        with importlib.resources.open_text(css, 'reporting.css') as css_file:
            style(css_file.read())
    with doc:
        with div(cls='cloud_box'):
            h1(f'CLOUDWASH REPORT - {provider}')
            for region_data in all_data:
                data = resourcewise_data(region_data)
                # Check if there is any data to display, else skip creating the table
                if any(data[key] for key in data.keys() if key not in non_rt_keys):
                    with table(id='cloud_table'):
                        tab_caption = table_caption(data)
                        caption(tab_caption)
                        with tbody():
                            for table_head in data.keys():
                                if data[table_head] and table_head not in non_rt_keys:
                                    with tr():
                                        td(table_head.replace("_", " ").title())
                                        bullet = '&#8226;'
                                        tab = '&nbsp;'
                                        line_break = '<br>'
                                        if isinstance(data[table_head], list):
                                            component = ''
                                            for resource_name in data[table_head]:
                                                component += bullet + ' ' + resource_name + ' '
                                            if component:
                                                td(raw(component))
                                        elif isinstance(data[table_head], dict):
                                            component = []
                                            for rtype, resources in data[table_head].items():
                                                rtype_line = bullet + rtype + line_break
                                                if len(resources):
                                                    component.append(rtype_line)
                                                    comp_line = tab * 2 + ' '
                                                    for resource_name in resources:
                                                        comp_line += (
                                                            bullet + ' ' + resource_name + ' '
                                                        )
                                                    comp_line += line_break
                                                    component.append(comp_line)
                                            joined_component = ' '.join(component)
                                            if joined_component:
                                                td(raw(joined_component))
                                        else:
                                            header = bullet + ' ' + data[table_head]
                                            td(raw(header))
    with open(f'cleanup_resource_{provider}.html', 'w') as file:
        file.write(doc.render())


def total_running_time(vm_obj) -> namedtuple:
    """Calculates the VMs total running time

    :param ComputeResource.vm vm_obj: Instance of a VM from any compute resource
    :return: The total running time in seconds, minutes and hours
    """
    if vm_obj.creation_time is None:
        return None
    start_time = vm_obj.creation_time.astimezone(pytz.UTC)
    now_time = datetime.now().astimezone(pytz.UTC)
    timediff = now_time - start_time
    totalseconds = timediff.total_seconds()
    totalTime = namedtuple("TotalTime", ["seconds", "minutes", "hours"])
    return totalTime(seconds=totalseconds, minutes=totalseconds / 60, hours=totalseconds / 3600)


def gce_zones() -> list:
    """Returns the list of GCE zones"""
    _bcds = dict.fromkeys(["us-east1", "europe-west1"], ["b", "c", "d"])
    _abcfs = dict.fromkeys(["us-central1"], ["a", "b", "c", "f"])
    _abcs = dict.fromkeys(
        [
            "us-east4",
            "us-west1",
            "europe-west4",
            "europe-west3",
            "europe-west2",
            "asia-east1",
            "asia-southeast1",
            "asia-northeast1",
            "asia-south1",
            "australia-southeast1",
            "southamerica-east1",
            "asia-east2",
            "asia-northeast2",
            "europe-north1",
            "europe-west6",
            "northamerica-northeast1",
            "us-west2",
        ],
        ["a", "b", "c"],
    )
    _zones_combo = {**_bcds, **_abcfs, **_abcs}
    zones = [f"{loc}-{zone}" for loc, zones in _zones_combo.items() for zone in zones]
    return zones


def calculate_time_threshold(time_ref=""):
    """Parses a time reference for data filtering

    :param str time_ref: a relative time reference for indicating the filter value
    of a relative time, given in a {time_value}{time_unit} format; default is "" (no filtering)
    :return datetime time_threshold
    """
    if time_ref is None:
        time_ref = ""

    if time_ref.isnumeric():
        # Use default time value as Minutes
        time_ref += "m"

    # Time Ref is Optional; if empty, time_threshold will be set as "now"
    time_threshold = dateparser.parse(f"now-{time_ref}-UTC")
    logger.debug(
        f"\nAssociated OCP resources are filtered by last creation time of: {time_threshold}"
    )
    return time_threshold


def are_resources_older_than(
    time_ref: str,
    resources: list[ResourceExplorerResource] = None,
) -> bool:
    """Check if all resources in the list were last modified before the SLA threshold.

    :param str time_ref: Relative time reference in {value}{unit} format (e.g. "7d", "1h")
    :param list resources: AWS resources to check against the time threshold
    :return: True if every resource was last modified before the threshold
    """
    time_threshold = calculate_time_threshold(time_ref=time_ref)
    return all(r.date_modified <= time_threshold for r in resources)


def check_installer_exists():
    """Verify the openshift-install CLI is available on PATH, exit if not found."""
    try:
        subprocess.run(
            ['openshift-install', '--help'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        logger.info("Found openshift-install CLI")
    except FileNotFoundError:
        logger.exception(
            "openshift-install CLI not found. "
            "Use the Docker container environment or install locally from: "
            "https://mirror.openshift.com/pub/openshift-v4/x86_64/"
            "clients/ocp/stable/openshift-install-linux.tar.gz"
            "\nFor more information: https://github.com/openshift/installer"
        )
        exit(1)


def destroy_ocp_cluster(metadata_path: str, cluster_name: str):
    """Run openshift-install destroy cluster using the provided metadata file.

    :param str metadata_path: Path to the metadata.json file for the cluster
    :param str cluster_name: Human-readable cluster name for logging
    """
    metadata = Path(metadata_path)
    if not metadata.exists():
        logger.error(f"Failed to load cluster info from metadata path: {metadata_path}")
        return

    cleanup_dir = str(metadata.parent)
    try:
        logger.info(f"Starting to destroy OCP cluster: {cluster_name}")
        result = subprocess.run(
            [
                'openshift-install',
                'destroy',
                'cluster',
                '--dir',
                cleanup_dir,
                '--log-level=debug',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(f"Failed to cleanup OCP cluster {cluster_name}:\n{result.stdout}")
        else:
            logger.debug(result.stdout)
            logger.info(f"Successfully destroyed OCP cluster: {cluster_name}")
    except subprocess.SubprocessError as ex:
        logger.error(f"Failed to cleanup OCP cluster {cluster_name}:\n{ex}")


def write_metadata_file(cluster_metadata: dict, cleanup_dir: str) -> str:
    """Write cluster metadata JSON required by openshift-install.

    :param dict cluster_metadata: Metadata dict with clusterName, clusterID, infraID, aws info
    :param str cleanup_dir: Directory to write the metadata.json into
    :return: Path to the written metadata file
    """
    metadata_file = Path(cleanup_dir) / "metadata.json"
    metadata_file.write_text(json.dumps(cluster_metadata))
    logger.debug(f"Metadata written to {metadata_file}")
    return str(metadata_file)
