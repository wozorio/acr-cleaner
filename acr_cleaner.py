#!/usr/bin/env python3

"""ACR cleaner

A script to clean up an Azure container registry by deleting dangling images and images
which are older than a specified period of time (in days) if they are not being used.
"""

__author__ = "Wellington Ozorio <well.ozorio@gmail.com>"


import dataclasses
import datetime
import logging
import os
import re
import subprocess
import sys
from ast import literal_eval

import humanize
from azure.containerregistry import ArtifactManifestOrder, ContainerRegistryClient
from azure.identity import EnvironmentCredential
from colorlog import ColoredFormatter

REQUIRED_ENVIRONMENT_VARIABLES = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"]

IMAGE_ID_PATTERN = re.compile(
    r"^(?P<registry>[a-z]+.[a-z]+.[a-z].)/(?P<repository>[a-z0-9-/]+)@(?P<digest>[sha256:]+[a-fA-F0-9]{64}$)"
)

AUDIENCE = "https://management.azure.com"

# Repositories containing images that are used by jobs are set as exceptions because pods created by
# jobs only exist when the job is running.
# Since jobs are triggered based on events, it's very unlikely that when the registry cleanup script
# is triggered, a job will also be running to ensure the respective image is not incorrectly deleted.
REPOS_WITH_JOB_IMAGES = ["ingress-nginx/kube-webhook-certgen"]

logger = logging.getLogger(__name__)


# pylint: disable=too-many-instance-attributes
@dataclasses.dataclass
class Arguments:
    """Represent the required environment variables and arguments passed from the command-line."""

    # Environment variables
    azure_client_id: str
    azure_client_secret: str
    azure_tenant_id: str
    azure_subscription_id: str
    # Positional arguments
    registry_name: str
    registry_resource_group: str
    max_image_age_days: int
    deployed_images: list[str]
    cleanup_all: bool


@dataclasses.dataclass
class Image:
    """Represent an image object."""

    repository: str
    tags: list[str]
    digest: str
    age: int
    is_dangling: bool = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.is_dangling = bool(self.tags)


def main(args: list[str], environ: dict) -> None | int:
    """The main function."""
    setup_logging()

    args = parse_args(args, environ)
    if args is None:
        return 1

    registry_uri = f"https://{args.registry_name}.azurecr.io"

    if args.cleanup_all:
        logger.warning("All images except the currently deployed ones will be deleted")
        max_image_age_days = 0
    else:
        max_image_age_days = args.max_image_age_days

    logger.warning(
        "Dangling images and unused images older than %i days will be deleted from the %s container registry",
        max_image_age_days,
        registry_uri,
    )

    with ContainerRegistryClient(registry_uri, EnvironmentCredential(), audience=AUDIENCE) as acr_client:
        obsolete_images = fetch_obsolete_images(acr_client, registry_uri, max_image_age_days, args.deployed_images)

        if not obsolete_images:
            logger.info("No obsolete images found for deletion")
            return None

        dangling_images = [image for image in obsolete_images if image.is_dangling]
        unused_images = [image for image in obsolete_images if not image.is_dangling]

        logger.warning("A total of %s dangling images will be deleted", humanize.intcomma(len(dangling_images)))
        logger.warning("A total of %s unused images will be deleted", humanize.intcomma(len(unused_images)))

        login_to_azure(args.azure_client_id, args.azure_client_secret, args.azure_tenant_id, args.azure_subscription_id)

        registry_usage_before_cleanup = get_registry_usage(args.registry_name, args.registry_resource_group)
        delete_obsolete_images(acr_client, obsolete_images)
        registry_usage_after_cleanup = get_registry_usage(args.registry_name, args.registry_resource_group)

        storage_released = registry_usage_before_cleanup - registry_usage_after_cleanup

        logger.info(
            "A total of %s has been released from the %s container registry",
            humanize.naturalsize(storage_released),
            args.registry_name,
        )
    return None


def setup_logging() -> None:
    """Set up a custom logger."""
    handler = logging.StreamHandler()
    formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(blue)s%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",  # ISO-8601 format
        reset=True,
        log_colors={"DEBUG": "cyan", "INFO": "green", "WARNING": "yellow", "ERROR": "red"},
        style="%",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel("INFO")


def parse_args(args: list[str], environ: dict) -> Arguments:
    """Parse command-line arguments."""
    if len(args) != 5 or "-h" in args or "--help" in args:
        print(
            "Usage: cleanup_acr.py REGISTRY_NAME REGISTRY_RESOURCE_GROUP MAX_IMAGE_AGE DEPLOYED_IMAGES CLEANUP_ALL",
            file=sys.stderr,
        )
        return

    check_env_vars(environ)
    args = Arguments(
        azure_client_id=environ["AZURE_CLIENT_ID"],
        azure_client_secret=environ["AZURE_CLIENT_SECRET"],
        azure_subscription_id=environ["AZURE_SUBSCRIPTION_ID"],
        azure_tenant_id=environ["AZURE_TENANT_ID"],
        registry_name=args[0],
        registry_resource_group=args[1],
        max_image_age_days=int(args[2]),
        deployed_images=args[3].split(","),
        cleanup_all=literal_eval(args[4]),
    )

    for image_id in args.deployed_images:
        validate_image_id(image_id)

    return args


def check_env_vars(environ: dict) -> None:
    """Check whether required environment variables are set."""
    for variable in REQUIRED_ENVIRONMENT_VARIABLES:
        if not variable in environ:
            raise RuntimeError(f"{variable} environment variable not set")


def fetch_obsolete_images(
    acr_client: ContainerRegistryClient, registry_uri: str, max_image_age_days: int, deployed_images: list[str]
) -> list[Image]:
    """Fetch a list of dangling images and unused images which are older than the `max_image_age_days` parameter."""
    images_in_use = []
    obsolete_images = []

    logger.info("Fetching list of dangling images and unused images older than %i days:", max_image_age_days)
    for repository in acr_client.list_repository_names():
        if not is_exception_repository(repository):
            logger.info("-> Checking repository %s", repository)
            for manifest in acr_client.list_manifest_properties(
                repository, order_by=ArtifactManifestOrder.LAST_UPDATED_ON_DESCENDING
            ):
                today = datetime.datetime.now(datetime.timezone.utc)
                image_last_update = manifest.last_updated_on

                image_age_days = (today - image_last_update).days

                if manifest.tags is None or (today - image_last_update) > datetime.timedelta(days=max_image_age_days):
                    image_id = registry_uri.removeprefix("https://") + "/" + repository + "@" + manifest.digest
                    validate_image_id(image_id)

                    if image_id in deployed_images:
                        images_in_use.append(
                            Image(
                                repository=repository,
                                tags=manifest.tags,
                                digest=manifest.digest,
                                age=image_age_days,
                            )
                        )
                    else:
                        obsolete_images.append(
                            Image(
                                repository=repository,
                                tags=manifest.tags,
                                digest=manifest.digest,
                                age=image_age_days,
                            )
                        )
    if images_in_use:
        logger.info(
            "The images below are older than %i days but they are in use, therefore they will not be deleted:",
            max_image_age_days,
        )
        for image in images_in_use:
            logger.info("%s/%s:%s@%s", registry_uri, image.repository, image.tags, image.digest)

    return obsolete_images


def login_to_azure(client_id: str, client_secret: str, tenant_id: str, subscription_id: str) -> None:
    """Login to Azure for subsequent azure-cli commands."""
    logger.info("Logging in to Azure")
    run_os_command(
        [
            "az",
            "login",
            "--service-principal",
            "-u",
            client_id,
            "-p",
            client_secret,
            "--tenant",
            tenant_id,
        ]
    )
    select_subscription(subscription_id)


def select_subscription(subscription_id: str) -> None:
    """Set an Azure subscription as active."""
    logger.info("Setting Azure subscription %s as active", subscription_id)
    run_os_command(["az", "account", "set", "--subscription", subscription_id])


def get_registry_usage(registry_name: str, resource_group: str) -> int:
    """Return the quota usage of the container registry."""
    quota_usage = run_os_command(
        [
            "az",
            "acr",
            "show-usage",
            "--name",
            registry_name,
            "--resource-group",
            resource_group,
            "--output",
            "json",
            "--query",
            "value[0].currentValue",
        ],
    )
    logger.info("The current quota usage on %s is %s", registry_name, humanize.naturalsize(quota_usage))

    return int(quota_usage)


def run_os_command(cmd: list[str]) -> str:
    """Wrapper to run OS commands."""
    try:
        output = subprocess.run(cmd, capture_output=True, check=True).stdout.decode()
    except subprocess.CalledProcessError as error:
        logger.exception(error)

    return output.strip()


def delete_obsolete_images(acr_client: ContainerRegistryClient, obsolete_images: list[Image]) -> None:
    """Delete all obsolete images passed in with the `obsolete_images` parameter."""
    for image in obsolete_images:
        logger.warning(
            "Deleting image %s:%s@%s. Image is %i days old.",
            image.repository,
            image.tags,
            image.digest,
            image.age,
        )
        acr_client.delete_manifest(image.repository, image.digest)


def is_exception_repository(repository: str) -> bool:
    """Check whether a repository should be an exception or not."""
    return repository.startswith("helm-charts") or (repository in REPOS_WITH_JOB_IMAGES)


def validate_image_id(image_id: str) -> None:
    """Check whether an image id has a valid format."""
    match = IMAGE_ID_PATTERN.match(image_id)

    if not match:
        raise RuntimeError(f"Image URI format {image_id} is not valid")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ) or 0)
