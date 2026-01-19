import logging
from django.utils import timezone
import swapper
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import gettext_lazy as _

from openwisp_utils.tasks import OpenwispCeleryTask

from . import settings as app_settings
from .exceptions import RecoverableFailure
from .swapper import load_model

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(RecoverableFailure,),
    soft_time_limit=app_settings.TASK_TIMEOUT,
    **app_settings.RETRY_OPTIONS,
)
def upgrade_firmware(self, operation_id):
    Schedule = load_model("DeviceFirmwareSchedule")

    try:
        operation = (
            load_model("UpgradeOperation")
            .objects.select_related("device")
            .get(pk=operation_id)
        )

        # mark op running
        if operation.status == "scheduled":
            operation.status = "in-progress"
            operation.save(update_fields=["status", "modified"])

        # mark schedule running (best-effort)
        try:
            sched = operation.device.devicefirmware.schedule
        except Exception:
            sched = None

        if sched and sched.status in ("scheduled", "pending"):
            sched.status = "running"
            sched.started_at = timezone.now()
            sched.save(update_fields=["status", "started_at", "modified"])

        recoverable = self.request.retries < self.max_retries
        operation.upgrade(recoverable=recoverable)

        # mirror final result to schedule
        if sched:
            final_map = {
                "success": "success",
                "failed": "failed",
                "aborted": "canceled",
            }
            sched.status = final_map.get(operation.status, sched.status)
            sched.finished_at = timezone.now()
            sched.save(update_fields=["status", "finished_at", "modified"])

    except SoftTimeLimitExceeded:
        operation.status = "failed"
        operation.log_line(_("Operation timed out."))
        logger.warning("SoftTimeLimitExceeded raised in upgrade_firmware task")

        # schedule fail (best-effort)
        try:
            sched = operation.device.devicefirmware.schedule
            sched.status = "failed"
            sched.finished_at = timezone.now()
            sched.save(update_fields=["status", "finished_at", "modified"])
        except Exception:
            pass

    except ObjectDoesNotExist:
        logger.warning(
            f"The UpgradeOperation object with id {operation_id} has been deleted"
        )


@shared_task(bind=True, soft_time_limit=app_settings.TASK_TIMEOUT)
def batch_upgrade_operation(self, batch_id, firmwareless):
    """
    Calls the ``batch_upgrade()`` method of a
    ``Build`` instance in the background
    """
    try:
        batch_operation = load_model("BatchUpgradeOperation").objects.get(pk=batch_id)
        batch_operation.upgrade(firmwareless=firmwareless)
    except SoftTimeLimitExceeded:
        batch_operation.status = "failed"
        batch_operation.save()
        logger.warning("SoftTimeLimitExceeded raised in batch_upgrade_operation task")
    except ObjectDoesNotExist:
        logger.warning(
            f"The BatchUpgradeOperation object with id {batch_id} has been deleted"
        )


@shared_task(base=OpenwispCeleryTask, bind=True)
def create_device_firmware(self, device_id):
    DeviceFirmware = load_model("DeviceFirmware")
    Device = swapper.load_model("config", "Device")

    qs = DeviceFirmware.objects.filter(device_id=device_id)
    if qs.exists():
        return

    device = Device.objects.get(pk=device_id)
    DeviceFirmware.create_for_device(device)


@shared_task(base=OpenwispCeleryTask, bind=True)
def create_all_device_firmwares(self, firmware_image_id):
    DeviceFirmware = load_model("DeviceFirmware")
    FirmwareImage = load_model("FirmwareImage")
    Device = swapper.load_model("config", "Device")

    fw_image = FirmwareImage.objects.select_related("build").get(pk=firmware_image_id)

    queryset = Device.objects.filter(os=fw_image.build.os)
    for device in queryset.iterator():
        DeviceFirmware.create_for_device(device, fw_image)
