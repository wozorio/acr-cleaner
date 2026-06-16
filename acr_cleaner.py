#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "azure-containerregistry",
#     "azure-identity",
#     "azure-mgmt-containerregistry",
#     "click",
#     "colorlog",
#     "humanize",
# ]
# ///

__author__ = "Wellington Ozorio <wozorio@duck.com>"

import dataclasses
import datetime
import logging
import os
import re
import sys

import click
import humanize
from azure.containerregistry import ArtifactManifestOrder, ContainerRegistryClient
from azure.identity import EnvironmentCredential
from azure.mgmt.containerregistry import ContainerRegistryManagementClient
from colorlog import ColoredFormatter

REQUIRED_ENVIRONMENT_VARIABLES = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"]

IMAGE_ID_PATTERN = re.compile(
    r"^(?P<registry>[a-z0-9-]+\.[a-z]+\.[a-z]+)/(?P<repository>[a-z0-9-/]+)@(?P<digest>sha256:[a-fA-F0-9]{64})$",
)

AUDIENCE = "https://management.azure.com"

# Repositories containing images that are used by jobs are set as exceptions because pods created by
# jobs only exist when the job is running.
# Since jobs are triggered based on events, it's very unlikely that when the registry cleanup script
# is triggered, a job will also be running to ensure the respective image is not incorrectly deleted.
REPOS_WITH_JOB_IMAGES = [
    "multiarch/qemu-user-static",
    "busybox",
    "cert-manager/jetstack/cert-manager-startupapicheck",
    "trust-manager/jetstack/trust-pkg-debian-bookworm",
    "linkerd/debug",
]

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Image:
    """Represent an image object."""

    repository: str
    tags: list[str]
    digest: str
    age: int


def validate_image_ids(value: str) -> list[str]:
    """Click custom parameter type to validate the format of provided image IDs."""
    images = value.split(",")
    for image in images:
        validate_image_id(image)
    return images


@click.command()
@click.argument("registry_name")
@click.argument("registry_resource_group")
@click.argument("max_image_age_days", type=int)
@click.argument("deployed_images", type=validate_image_ids)
def main(
    registry_name: str,
    registry_resource_group: str,
    max_image_age_days: int,
    deployed_images: list[str],
) -> None:
    """ACR cleaner script.

    A script to clean up Azure container registries by deleting unused
    images which are older than a specified period of time (in days).
    """
    setup_logging()

    check_env_vars(os.environ)

    registry_uri = f"https://{registry_name}.azurecr.io"

    credential = EnvironmentCredential()

    logger.warning(
        "Unused images older than %i days will be deleted from the %s container registry",
        max_image_age_days,
        registry_uri,
    )

    with ContainerRegistryClient(registry_uri, credential, audience=AUDIENCE) as acr_client:
        obsolete_images = fetch_obsolete_images(acr_client, registry_uri, max_image_age_days, deployed_images)

        if not obsolete_images:
            logger.info("No obsolete images found for deletion")
            return

        logger.warning("A total of %s unused images will be deleted", humanize.intcomma(len(obsolete_images)))

        subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]

        registry_usage_before_cleanup = get_registry_usage(
            credential,
            subscription_id,
            registry_name,
            registry_resource_group,
        )

        delete_obsolete_images(acr_client, obsolete_images)

        registry_usage_after_cleanup = get_registry_usage(
            credential,
            subscription_id,
            registry_name,
            registry_resource_group,
        )

        storage_released = registry_usage_before_cleanup - registry_usage_after_cleanup

        logger.info(
            "A total of %s has been released from the %s container registry",
            humanize.naturalsize(storage_released),
            registry_name,
        )


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


def check_env_vars(environ: dict) -> None:
    """Check whether required environment variables are set."""
    for variable in REQUIRED_ENVIRONMENT_VARIABLES:
        if variable not in environ:
            logger.error("%s environment variable not set", variable)
            sys.exit(1)


def fetch_obsolete_images(
    acr_client: ContainerRegistryClient,
    registry_uri: str,
    max_image_age_days: int,
    deployed_images: list[str],
) -> list[Image]:
    """Fetch a list of unused images which are older than the `max_image_age_days` parameter."""
    images_in_use: list[Image] = []
    obsolete_images: list[Image] = []

    expanded_deployed_images = expand_deployed_images(acr_client, deployed_images)
    logger.info("Fetching list of unused images older than %i days:", max_image_age_days)

    for repository in acr_client.list_repository_names():
        if not is_exception_repository(repository):
            logger.info("-> Checking repository %s", repository)
            manifests = acr_client.list_manifest_properties(
                repository,
                order_by=ArtifactManifestOrder.LAST_UPDATED_ON_DESCENDING,
            )
            for manifest in manifests:
                today = datetime.datetime.now(datetime.timezone.utc)
                image_last_update = manifest.last_updated_on
                image_age_days = (today - image_last_update).days

                if (today - image_last_update) > datetime.timedelta(days=max_image_age_days):
                    image_id = f"{registry_uri.removeprefix('https://')}/{repository}@{manifest.digest}"
                    validate_image_id(image_id)

                    image = Image(
                        repository=repository,
                        tags=manifest.tags,
                        digest=manifest.digest,
                        age=image_age_days,
                    )
                    if image_id in expanded_deployed_images:
                        images_in_use.append(image)
                    else:
                        obsolete_images.append(image)

    if images_in_use:
        logger.info(
            "The images below are older than %i days but they are in use, therefore they will not be deleted:",
            max_image_age_days,
        )
        for image in images_in_use:
            logger.info("%s/%s:%s@%s", registry_uri, image.repository, image.tags, image.digest)

    return obsolete_images


def expand_deployed_images(acr_client: ContainerRegistryClient, deployed_images: list[str]) -> list[str]:
    """Expand deployed images to also include child manifests referenced by manifest lists (indexes).

    When a multi-arch image is deployed, Kubernetes reports the parent manifest list (index) digest as the
    image ID. But the registry also stores individual child manifests (architecture & attestation) that have
    no tags of their own.

    Without this expansion those child manifests would not be found in the
    deployed_images list and would be incorrectly treated as obsolete.
    """
    logger.info("Expanding deployed images to include child architecture & attestation manifests:")
    expanded_deployed_images = list(deployed_images)

    for image_id in deployed_images:
        registry_and_repository, digest = image_id.split("@")
        _, repository = registry_and_repository.split("/", 1)

        index_manifest = acr_client.get_manifest(repository, digest)
        child_manifests = index_manifest.manifest.get("manifests", [])

        for child_manifest in child_manifests:
            child_digest = child_manifest.get("digest")
            logger.info("-> Child digest %s found for deployed image %s", child_digest, image_id)
            child_image_id = f"{registry_and_repository}@{child_digest}"
            validate_image_id(child_image_id)
            expanded_deployed_images.append(child_image_id)

    return expanded_deployed_images


def get_registry_usage(
    credential: EnvironmentCredential,
    subscription_id: str,
    registry_name: str,
    resource_group: str,
) -> int:
    """Return the storage quota usage of the container registry using the Management SDK."""
    client = ContainerRegistryManagementClient(credential=credential, subscription_id=subscription_id)

    usages = client.registries.list_usages(resource_group, registry_name)
    quota_usage = next((item["currentValue"] for item in usages["value"] if item["name"] == "Size"), None)

    logger.info("The current quota usage on %s is %s", registry_name, humanize.naturalsize(quota_usage))
    return int(quota_usage)


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
    return repository.startswith("e2e-tests") or (repository in REPOS_WITH_JOB_IMAGES)


def validate_image_id(image_id: str) -> None:
    """Check whether an image id has a valid format."""
    match = IMAGE_ID_PATTERN.match(image_id)

    if not match:
        logger.error("Image URI format %s is not valid", image_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
