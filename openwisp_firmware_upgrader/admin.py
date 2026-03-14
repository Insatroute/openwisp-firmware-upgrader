import json
import logging
import json
import logging
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.utils.formats import date_format
import reversion
import swapper
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.serializers.json import DjangoJSONEncoder
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.urls import path, resolve, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.timezone import localtime
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from reversion.admin import VersionAdmin

from openwisp_controller.config.admin import DeactivatedDeviceReadOnlyMixin, DeviceAdmin
from openwisp_users.multitenancy import MultitenantAdminMixin, MultitenantOrgFilter
from django.contrib.admin.views.decorators import staff_member_required

from openwisp_utils.admin import ReadOnlyAdmin, TimeReadonlyAdminMixin
from openwisp_firmware_upgrader.tasks import batch_upgrade_operation
from .filters import (
    BuildCategoryFilter,
    BuildCategoryOrganizationFilter,
    CategoryFilter,
    CategoryOrganizationFilter,
)
from .swapper import load_model
from .utils import get_upgrader_schema_for_device
from .widgets import FirmwareSchemaWidget
from celery import app as celery_app

logger = logging.getLogger(__name__)
BatchUpgradeOperation = load_model("BatchUpgradeOperation")
UpgradeOperation = load_model("UpgradeOperation")
DeviceFirmware = load_model("DeviceFirmware")
DeviceFirmwareSchedule = load_model("DeviceFirmwareSchedule")  # ✅ added
FirmwareImage = load_model("FirmwareImage")
Category = load_model("Category")
Build = load_model("Build")
Device = swapper.load_model("config", "Device")
DeviceConnection = swapper.load_model("connection", "DeviceConnection")
from django.db import transaction
from django.utils import timezone
# from openwisp_controller.config.models import DeviceGroup


class BaseAdmin(MultitenantAdminMixin, TimeReadonlyAdminMixin, admin.ModelAdmin):
    save_on_top = True


class BaseVersionAdmin(MultitenantAdminMixin, TimeReadonlyAdminMixin, VersionAdmin):
    history_latest_first = True
    save_on_top = True

from django.db.models import Count
from django.utils.http import urlencode
# @admin.register(load_model("Category"))
# class CategoryAdmin(BaseVersionAdmin):
#     list_display = ["name", "organization", "total_devices", "created", "modified"]
#     list_filter = [MultitenantOrgFilter]
#     list_select_related = ["organization"]
#     search_fields = ["name"]
#     ordering = ["-name", "-created"]
    
#     def get_queryset(self, request):
#         qs = super().get_queryset(request)
#         return qs.annotate(_device_count=Count("organization__device", distinct=True))


#     @admin.display(description="Devices", ordering="_device_count")
#     def total_devices(self, obj):
#         count = getattr(obj, "_device_count", 0) or 0
#         if count == 0:
#             return "0"

#         # Link to Device changelist filtered by organization (recommended)
#         device_cl = reverse(f"admin:{Device._meta.app_label}_{Device._meta.model_name}_changelist")

#         # Most common field name on Device is `organization` (FK to organization).
#         params = urlencode({"organization__id__exact": obj.organization_id})
#         return format_html('<a href="{}?{}">{}</a>', device_cl, params, count)

class CategoryAdminForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # organization can come from:
        # - POST (self.data)
        # - GET add page initial (self.initial)
        # - change page instance
        org_id = (
            self.data.get("organization")
            or self.initial.get("organization")
            or getattr(self.instance, "organization_id", None)
        )

        if org_id in (None, "", "None", "null"):
            org_id = None
        
        # Choose ONE behavior for systemwide categories (org=None):
        # A) show all devices:
        if org_id is None:
            qs = Device.objects.all()
        else:
            qs = Device.objects.filter(organization_id=org_id)

        self.fields["devices"].queryset = qs.order_by("name")


@admin.register(Category)
class CategoryAdmin(BaseVersionAdmin):
    form = CategoryAdminForm

    list_display = ["name", "organization", "total_devices", "created", "modified"]
    list_filter = [MultitenantOrgFilter]
    list_select_related = ["organization"]
    search_fields = ["name"]
    ordering = ["-name", "-created"]
    filter_horizontal = ("devices",)

    class Media:
        js = (
            "firmware-upgrader/js/category-selected-device.js",
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "devices-by-org/",
                self.admin_site.admin_view(self.devices_by_org),
                name="firmware_category_devices_by_org",
            ),
        ]
        return custom + urls

    def devices_by_org(self, request):
        org_id = request.GET.get("org_id")

        if org_id in (None, "", "None", "null"):
            org_id = None

        # Must match form behavior for org=None:
        if org_id is None:
            qs = Device.objects.all().order_by("name")
        else:
            qs = Device.objects.filter(organization_id=org_id).order_by("name")

        return JsonResponse(
            {"results": [{"id": str(d.pk), "text": str(d)} for d in qs]}
        )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(_device_count=Count("devices", distinct=True))

    @admin.display(description="Devices", ordering="_device_count")
    def total_devices(self, obj):
        count = getattr(obj, "_device_count", 0) or 0
        if count == 0:
            return "0"

        device_cl = reverse(
            f"admin:{Device._meta.app_label}_{Device._meta.model_name}_changelist"
        )
        params = urlencode({"firmware_categories": obj.id})
        return format_html('<a href="{}?{}">{}</a>', device_cl, params, count)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        """
        Change page: enforce device queryset to the instance org.
        Add page: ModelForm __init__ handles it (POST/initial).
        """
        if db_field.name == "devices":
            obj_id = request.resolver_match.kwargs.get("object_id")
            if obj_id:
                try:
                    cat = Category.objects.only("organization_id").get(pk=obj_id)
                    if cat.organization_id:
                        kwargs["queryset"] = Device.objects.filter(
                            organization_id=cat.organization_id
                        ).order_by("name")
                    else:
                        kwargs["queryset"] = Device.objects.all().order_by("name")
                except Category.DoesNotExist:
                    pass

        return super().formfield_for_manytomany(db_field, request, **kwargs)

class FirmwareImageInline(TimeReadonlyAdminMixin, admin.StackedInline):
    model = FirmwareImage
    extra = 0

    class Media:
        extra = "" if getattr(settings, "DEBUG", False) else ".min"
        i18n_name = admin.widgets.SELECT2_TRANSLATIONS.get(get_language())
        i18n_file = (
            ("admin/js/vendor/select2/i18n/%s.js" % i18n_name,) if i18n_name else ()
        )
        js = (
            (
                "admin/js/vendor/jquery/jquery%s.js" % extra,
                "admin/js/vendor/select2/select2.full%s.js" % extra,
            )
            + i18n_file
            + ("admin/js/jquery.init.js", "firmware-upgrader/js/build.js")
        )

        css = {
            "screen": ("admin/css/vendor/select2/select2%s.css" % extra,),
        }

    def has_change_permission(self, request, obj=None):
        if obj:
            return False
        return True

from django import forms
class AdminSplitDateTimePicker(forms.SplitDateTimeWidget):
    def __init__(self, attrs=None):
        super().__init__(attrs=attrs)

        # date input (adds right margin)
        self.widgets[0] = forms.DateInput(
            attrs={
                "type": "date",
                "style": "width:170px; margin-left:12px;",  # ✅ spacing
            }
        )

        # time input
        self.widgets[1] = forms.TimeInput(
            attrs={
                "type": "time",
                "step": "60",
                "style": "width:170px; margin-left:12px;",  # keep clean
            },
            format="%H:%M",
        )

class BatchUpgradeConfirmationForm(forms.ModelForm):
    upgrade_options = forms.JSONField(widget=FirmwareSchemaWidget(), required=False)
    scheduled_at = forms.SplitDateTimeField(
        required=False,
        widget=AdminSplitDateTimePicker(),
        help_text=_(
            "Choose a date and time to schedule the firmware upgrade for all devices. "
            "If left empty, the upgrade will start immediately."
        )
    )
    build = forms.ModelChoiceField(
        widget=forms.HiddenInput(), required=False, queryset=Build.objects.all()
    )

    class Meta:
        model = BatchUpgradeOperation
        fields = ("build", "upgrade_options", "scheduled_at")
        
    def clean_scheduled_at(self):
        dt = self.cleaned_data.get("scheduled_at")
        if not dt:
            return dt

        now = timezone.now()
        max_dt = now + timezone.timedelta(days=15)

        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())

        if dt < now:
            raise ValidationError(_("Scheduled time must be from now onwards."))
        if dt > max_dt:
            raise ValidationError(_("You can schedule only within the next 15 days."))
        return dt

    @property
    def media(self):
        js = [
            "firmware-upgrader/js/upgrade-selected-confirmation.js",
        ]
        css = {"all": ["firmware-upgrader/css/upgrade-selected-confirmation.css"]}
        return super().media + forms.Media(js=js, css=css)


@admin.register(load_model("Build"))
class BuildAdmin(BaseAdmin):
    list_display = ["__str__", "organization", "category", "created", "modified"]
    list_filter = [CategoryOrganizationFilter, CategoryFilter]
    list_select_related = ["category", "category__organization"]
    search_fields = ["category__name", "version", "os"]
    ordering = ["-created", "-version"]
    inlines = [FirmwareImageInline]
    actions = ["upgrade_selected"]
    multitenant_parent = "category"
    autocomplete_fields = ["category"]

    # Allows apps that extend this modules to use this template with less hacks
    change_form_template = "admin/firmware_upgrader/change_form.html"
    
    def _schedule_batch_upgrade(self, batch, scheduled_at, firmwareless):
        if timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())

        transaction.on_commit(
            lambda: batch_upgrade_operation.apply_async(
                args=[str(batch.pk), firmwareless],
                eta=scheduled_at,
            )
        )
    def organization(self, obj):
        return obj.category.organization

    organization.short_description = _("organization")

    @admin.action(
        description=_("Mass-upgrade devices related to the selected build"),
        permissions=["change"],
    )
    # def upgrade_selected(self, request, queryset):
    #     opts = self.model._meta
    #     app_label = opts.app_label
    #     # multiple concurrent batch upgrades are not supported
    #     # (it's not yet possible to select more builds and upgrade
    #     #  all of them at the same time)
    #     if queryset.count() > 1:
    #         self.message_user(
    #             request,
    #             _(
    #                 "Multiple mass upgrades requested but at the moment only "
    #                 "a single mass upgrade operation at time is supported."
    #             ),
    #             messages.ERROR,
    #         )
    #         # returning None will display the change list page again
    #         return None
    #     upgrade_all = request.POST.get("upgrade_all")
    #     upgrade_related = request.POST.get("upgrade_related")
    #     upgrade_options = request.POST.get("upgrade_options")
    #     form = BatchUpgradeConfirmationForm()
    #     build = queryset.first()
    #     # upgrade has been confirmed
    #     if upgrade_all or upgrade_related:
    #         scheduled_at_0 = request.POST.get("scheduled_at_0")  # date part
    #         scheduled_at_1 = request.POST.get("scheduled_at_1")  # time part
    #         form = BatchUpgradeConfirmationForm(
    #             data={"upgrade_options": upgrade_options, "build": build.pk, "scheduled_at_0": scheduled_at_0,
    #         "scheduled_at_1": scheduled_at_1,}
    #         )
    #         form.full_clean()
    #         if not form.errors:
    #             upgrade_options = form.cleaned_data["upgrade_options"]
    #             scheduled_at = form.cleaned_data["scheduled_at"]
    #             batch = build.batch_upgrade(
    #                 firmwareless=upgrade_all,
    #                 upgrade_options=upgrade_options,
    #             )

    #             scheduled_at = form.cleaned_data.get("scheduled_at")
    #             firmwareless = bool(upgrade_all)  # upgrade_all => include firmwareless devices
    #             if scheduled_at:
    #                 self._schedule_batch_upgrade(batch, scheduled_at, firmwareless)

    #             text = _(
    #                 "You can track the progress of this mass upgrade operation "
    #                 "in this page. Refresh the page from time to time to check "
    #                 "its progress."
    #             )
    #             self.message_user(request, mark_safe(text), messages.SUCCESS)
    #             url = reverse(
    #                 f"admin:{app_label}_batchupgradeoperation_change", args=[batch.pk]
    #             )
    #             return redirect(url)
    #     # upgrade needs to be confirmed
    #     result = BatchUpgradeOperation.dry_run(build=build)
    #     related_device_fw = result["device_firmwares"]
    #     firmwareless_devices = result["devices"]
    #     title = _("Confirm mass upgrade operation")
    #     context = self.admin_site.each_context(request)
    #     upgrader_schema = BatchUpgradeOperation(build=build)._get_upgrader_schema(
    #         related_device_fw=related_device_fw,
    #         firmwareless_devices=firmwareless_devices,
    #     )

    #     context.update(
    #         {
    #             "title": title,
    #             "related_device_fw": related_device_fw,
    #             "related_count": len(related_device_fw),
    #             "firmwareless_devices": firmwareless_devices,
    #             "firmwareless_count": len(firmwareless_devices),
    #             "form": form,
    #             "firmware_upgrader_schema": json.dumps(
    #                 upgrader_schema, cls=DjangoJSONEncoder
    #             ),
    #             "build": build,
    #             "opts": opts,
    #             "action_checkbox_name": ACTION_CHECKBOX_NAME,
    #             "media": self.media,
    #         }
    #     )
    #     request.current_app = self.admin_site.name
    #     return TemplateResponse(
    #         request,
    #         [
    #             "admin/%s/%s/upgrade_selected_confirmation.html"
    #             % (app_label, opts.model_name),
    #             "admin/%s/upgrade_selected_confirmation.html" % app_label,
    #             "admin/upgrade_selected_confirmation.html",
    #         ],
    #         context,
    #     )
    def upgrade_selected(self, request, queryset):
        opts = self.model._meta
        app_label = opts.app_label

        if queryset.count() > 1:
            self.message_user(
                request,
                _(
                    "Multiple mass upgrades requested but at the moment only "
                    "a single mass upgrade operation at time is supported."
                ),
                messages.ERROR,
            )
            return None
        upgrade_all = request.POST.get("upgrade_all")
        upgrade_related = request.POST.get("upgrade_related")
        upgrade_options_raw = request.POST.get("upgrade_options")
        build = queryset.first()

        # ✅ POST: confirmed
        if upgrade_all or upgrade_related:
            scheduled_at_0 = request.POST.get("scheduled_at_0")
            scheduled_at_1 = request.POST.get("scheduled_at_1")
            form = BatchUpgradeConfirmationForm(
                data={
                    "upgrade_options": upgrade_options_raw,
                    "build": build.pk,
                    "scheduled_at_0": scheduled_at_0,
                    "scheduled_at_1": scheduled_at_1,
                }
            )
            form.full_clean()
            if not form.errors:
                upgrade_options = form.cleaned_data.get("upgrade_options") or {}
                scheduled_at = form.cleaned_data.get("scheduled_at")

                firmwareless = bool(upgrade_all)  # upgrade_all => includes firmwareless

                # ✅ IMPORTANT:
                # If scheduled_at exists -> call scheduled batch_upgrade
                # else -> call immediate batch_upgrade
                batch = build.batch_upgrade(
                    firmwareless=firmwareless,
                    upgrade_options=upgrade_options,
                    scheduled_at=scheduled_at,   # ✅ your patched Build.batch_upgrade supports this
                )
                text = _(
                    "You can track the progress of this mass upgrade operation "
                    "in this page. Refresh the page from time to time to check "
                    "its progress."
                )
                self.message_user(request, mark_safe(text), messages.SUCCESS)
                url = reverse(
                    f"admin:{app_label}_batchupgradeoperation_change", args=[batch.pk]
                )
                return redirect(url)

            # if form invalid -> fall through and re-render confirmation below
        else:
            form = BatchUpgradeConfirmationForm()

        # ✅ GET: show confirmation page
        result = BatchUpgradeOperation.dry_run(build=build)
        related_device_fw = result["device_firmwares"]
        firmwareless_devices = result["devices"]
        title = _("Confirm mass upgrade operation")
        context = self.admin_site.each_context(request)
        upgrader_schema = BatchUpgradeOperation(build=build)._get_upgrader_schema(
            related_device_fw=related_device_fw,
            firmwareless_devices=firmwareless_devices,
        )

        context.update(
            {
                "title": title,
                "related_device_fw": related_device_fw,
                "related_count": len(related_device_fw),
                "firmwareless_devices": firmwareless_devices,
                "firmwareless_count": len(firmwareless_devices),
                "form": form,
                "firmware_upgrader_schema": json.dumps(
                    upgrader_schema, cls=DjangoJSONEncoder
                ),
                "build": build,
                "opts": opts,
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
                "media": self.media,
            }
        )
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            [
                "admin/%s/%s/upgrade_selected_confirmation.html" % (app_label, opts.model_name),
                "admin/%s/upgrade_selected_confirmation.html" % app_label,
                "admin/upgrade_selected_confirmation.html",
            ],
            context,
        )
    upgrade_selected.short_description = (
        "Mass-upgrade devices related " "to the selected build"
    )

    def change_view(self, request, object_id, form_url="", extra_context=None):
        app_label = self.model._meta.app_label
        extra_context = extra_context or {}
        upgrade_url = f"{app_label}_build_changelist"
        extra_context.update({"upgrade_url": upgrade_url})
        return super().change_view(request, object_id, form_url, extra_context)


class UpgradeOperationForm(forms.ModelForm):
    class Meta:
        fields = ["device", "image", "status", "log", "modified"]
        labels = {"modified": _("last updated")}


class UpgradeOperationInline(admin.StackedInline):
    model = UpgradeOperation
    form = UpgradeOperationForm
    readonly_fields = UpgradeOperationForm.Meta.fields
    extra = 0

    def has_delete_permission(self, request, obj):
        return False

    def has_add_permission(self, request, obj):
        return False

    class Media:
        css = {"all": ["firmware-upgrader/css/upgrade-options.css"]}


class ReadonlyUpgradeOptionsMixin:
    @admin.display(description=_("Upgrade options"))
    def readonly_upgrade_options(self, obj):
        upgrader_schema = obj.upgrader_schema
        if not upgrader_schema:
            return _("Upgrade options are not supported for this upgrader.")
        options = []
        for key, value in upgrader_schema["properties"].items():
            option_used = "yes" if obj.upgrade_options.get(key, False) else "no"
            option_title = value["title"]
            icon_url = static(f"admin/img/icon-{option_used}.svg")
            options.append(
                f'<li><img src="{icon_url}" alt="{option_used}">{option_title}</li>'
            )
        return format_html(
            mark_safe(f'<ul class="readonly-upgrade-options">{"".join(options)}</ul>')
        )


@admin.register(BatchUpgradeOperation)
class BatchUpgradeOperationAdmin(ReadonlyUpgradeOptionsMixin, ReadOnlyAdmin, BaseAdmin):
    list_display = ["build", "organization", "status", "created", "modified"]
    list_filter = [
        BuildCategoryOrganizationFilter,
        "status",
        BuildCategoryFilter,
    ]
    list_select_related = ["build__category__organization"]
    ordering = ["-created"]
    inlines = [UpgradeOperationInline]
    multitenant_parent = "build__category"
    fields = [
        "build",
        "status",
        "scheduled_at_display",
        "completed",
        "success_rate",
        "failed_rate",
        "aborted_rate",
        "readonly_upgrade_options",
        "created",
        "modified",
    ]
    autocomplete_fields = ["build"]
    readonly_fields = [
        "scheduled_at_display",
        "completed",
        "success_rate",
        "failed_rate",
        "aborted_rate",
        "readonly_upgrade_options",
    ]

    def organization(self, obj):
        return obj.build.category.organization

    organization.short_description = _("organization")

    def get_readonly_fields(self, request, obj):
        fields = super().get_readonly_fields(request, obj)
        return fields + self.__class__.readonly_fields

    def completed(self, obj):
        return obj.progress_report

    def success_rate(self, obj):
        return self.__get_rate(obj.success_rate)

    def failed_rate(self, obj):
        return self.__get_rate(obj.failed_rate)

    def aborted_rate(self, obj):
        return self.__get_rate(obj.aborted_rate)

    def __get_rate(self, value):
        if value:
            return f"{value}%"
        return "N/A"

    # @admin.display(description="Scheduled at")
    # def scheduled_at_display(self, obj):
    #     # schedule comes from BatchUpgradeOperationSchedule (OneToOne)
    #     sched = getattr(obj, "schedule", None)
    #     if not sched or not sched.scheduled_at:
    #         return "-"

    #     # show only if it is actually scheduled (future or status scheduled)
    #     scheduled_dt = sched.scheduled_at
    #     if timezone.is_naive(scheduled_dt):
    #         scheduled_dt = timezone.make_aware(scheduled_dt, timezone.get_current_timezone())

    #     scheduled_local = timezone.localtime(scheduled_dt)
    #     now_local = timezone.localtime(timezone.now())

    #     remaining = scheduled_local - now_local

    #     # if already running or past time
    #     if remaining <= timedelta(seconds=0):
    #         # If you want: show running/started instead of "-"
    #         return format_html(
    #             "{} <span style='color:#6b7280;'>(status: <b>{}</b>)</span>",
    #             date_format(scheduled_local, "DATETIME_FORMAT"),
    #             sched.status,
    #         )

    #     total_seconds = int(remaining.total_seconds())
    #     days, rem = divmod(total_seconds, 86400)
    #     hours, rem = divmod(rem, 3600)
    #     minutes, seconds = divmod(rem, 60)

    #     if days > 0:
    #         remaining_txt = f"{days}d {hours}h {minutes}m"
    #     elif hours > 0:
    #         remaining_txt = f"{hours}h {minutes}m"
    #     elif minutes > 0:
    #         remaining_txt = f"{minutes}m {seconds}s"
    #     else:
    #         remaining_txt = f"{seconds}s"

    #     return format_html(
    #         "{} <span style='color:#6b7280;'>(starts in <b>{}</b>, status: <b>{}</b>)</span>",
    #         date_format(scheduled_local, "DATETIME_FORMAT"),
    #         remaining_txt,
    #         sched.status,
    #     )
    @admin.display(description="Scheduled at")
    def scheduled_at_display(self, obj):
        # ✅ show only for scheduled batch
        if getattr(obj, "status", None) != "scheduled":
            return "-"

        # schedule comes from BatchUpgradeOperationSchedule (OneToOne)
        sched = getattr(obj, "schedule", None)
        if not sched or not sched.scheduled_at:
            return "-"

        scheduled_dt = sched.scheduled_at
        if timezone.is_naive(scheduled_dt):
            scheduled_dt = timezone.make_aware(
                scheduled_dt, timezone.get_current_timezone()
            )

        scheduled_local = timezone.localtime(scheduled_dt)
        now_local = timezone.localtime(timezone.now())

        remaining = scheduled_local - now_local

        if remaining <= timedelta(seconds=0):
            remaining_txt = "0s"
        else:
            total_seconds = int(remaining.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)

            if days > 0:
                remaining_txt = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                remaining_txt = f"{hours}h {minutes}m"
            elif minutes > 0:
                remaining_txt = f"{minutes}m {seconds}s"
            else:
                remaining_txt = f"{seconds}s"

        return format_html(
            "{} <span style='color:#6b7280;'>(starts in <b>{}</b>)</span>",
            date_format(scheduled_local, "DATETIME_FORMAT"),
            remaining_txt,
        )
        
    completed.short_description = _("completed")
    success_rate.short_description = _("success rate")
    failed_rate.short_description = _("failure rate")
    aborted_rate.short_description = _("abortion rate")

class DeviceFirmwareForm(forms.ModelForm):
    upgrade_options = forms.JSONField(widget=FirmwareSchemaWidget, required=False)
    scheduled_at = forms.SplitDateTimeField(
        required=False,
        widget=admin.widgets.AdminSplitDateTime(),
        # widget=AdminSplitDateTimePicker(),
        help_text=_(
            "Choose a date and time to schedule the firmware upgrade. "
            "If left empty, the upgrade will start immediately."
        )
    )
    def clean_scheduled_at(self):
        dt = self.cleaned_data.get("scheduled_at")
        if not dt:
            return dt

        now = timezone.now()
        max_dt = now + timezone.timedelta(days=15)

        # make aware if needed
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())

        if dt < now:
            raise ValidationError(_("Scheduled time must be from now onwards."))
        if dt > max_dt:
            raise ValidationError(_("You can schedule only within the next 15 days."))
        return dt

    class Meta:
        model = DeviceFirmware
        fields = ("device", "image", "installed", "upgrade_options", "scheduled_at")
        

    class Media:
        js = ["admin/js/jquery.init.js", "firmware-upgrader/js/device-firmware.js"]
        css = {"all": ["firmware-upgrader/css/device-firmware.css"]}

    def __init__(self, *args, **kwargs):
        device = kwargs.pop("device", None)
        super().__init__(*args, **kwargs)

        if device is not None:
            self.fields["image"].queryset = DeviceFirmware.get_image_queryset_for_device(
                device, device_firmware=self.instance
            )

        if self.instance and self.instance.pk:
            try:
                self.fields["scheduled_at"].initial = self.instance.schedule.scheduled_at
            except Exception:
                pass

    def _normalize_upgrade_options_post(self):
        """
        Fix: FirmwareSchemaWidget may submit multiple inputs with the same name
        (e.g. hidden + textarea), so request.POST contains a list.
        Django JSONField tries to call .strip() and crashes.
        """
        if not hasattr(self, "data") or self.data is None:
            return

        key = self.add_prefix("upgrade_options")
        values = self.data.getlist(key)
        if len(values) <= 1:
            return

        # pick last non-empty value
        chosen = ""
        for v in reversed(values):
            if v not in (None, "", "null"):
                chosen = v
                break

        qd = self.data.copy()  # QueryDict is immutable
        qd.setlist(key, [chosen])  # force single value
        self.data = qd

    def full_clean(self):
        self._normalize_upgrade_options_post()
        super().full_clean()

        # ✅ If inline is being deleted, do not run custom validation
        if getattr(self, "cleaned_data", None) and self.cleaned_data.get("DELETE"):
            return

        if self.errors or not hasattr(self, "cleaned_data"):
            return

        device = self.cleaned_data.get("device")
        image = self.cleaned_data.get("image")
        if not device or not image:
            return  # ✅ prevents KeyError

        upgrade_op = UpgradeOperation(
            device=device,
            image=image,
            upgrade_options=self.cleaned_data.get("upgrade_options") or {},
        )
        try:
            upgrade_op.full_clean()
        except forms.ValidationError as error:
            self.add_error("__all__", error.messages[0])

    def save(self, commit=True):
        if not commit:
            return self.instance

        from django.utils import timezone

        scheduled_at = self.cleaned_data.get("scheduled_at")
        upgrade_options = self.cleaned_data.get("upgrade_options") or {}

        # Make aware if needed (DO NOT localtime it; keep it comparable with timezone.now())
        if scheduled_at and timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(
                scheduled_at, timezone.get_current_timezone()
            )

        # This triggers scheduling in the model (transaction.on_commit)
        self.instance.save(
            upgrade_options=upgrade_options,
            scheduled_at=scheduled_at,
        )

        # Only persist scheduled_at for display; DO NOT touch status/task_id here
        sched, _ = DeviceFirmwareSchedule.objects.get_or_create(
            device_firmware=self.instance
        )
        if sched.scheduled_at != scheduled_at:
            sched.scheduled_at = scheduled_at
            sched.save(update_fields=["scheduled_at"])

        return self.instance




class DeviceFormSet(forms.BaseInlineFormSet):
    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["device"] = self.instance
        return kwargs

    # def delete_existing(self, obj, commit=True):
    #     """
    #     Called when inline row is marked for DELETE.
    #     If it's a scheduled upgrade -> cancel celery task and clear schedule row.
    #     """
    #     if getattr(obj, "status", None) == "scheduled":
    #         df = getattr(obj.device, "devicefirmware", None)
    #         sched = getattr(df, "schedule", None) if df else None

    #         # revoke celery ETA task (best effort)
    #         if sched and sched.celery_task_id:
    #             try:
    #                 celery_app.control.revoke(sched.celery_task_id, terminate=False)
    #             except Exception:
    #                 pass

    #         # clear schedule record
    #         if sched:
    #             sched.status = "canceled"
    #             sched.scheduled_at = None
    #             sched.celery_task_id = ""
    #             sched.save(update_fields=["status", "scheduled_at", "celery_task_id"])
    #     return super().delete_existing(obj, commit=commit)
    def delete_existing(self, obj, commit=True):
        # obj is DeviceFirmware
        sched = getattr(obj, "schedule", None)

        if sched and sched.status == "scheduled" and sched.celery_task_id:
            try:
                celery_app.control.revoke(sched.celery_task_id, terminate=False)
            except Exception:
                pass

            sched.status = "canceled"
            sched.scheduled_at = None
            sched.celery_task_id = ""
            sched.save(update_fields=["status", "scheduled_at", "celery_task_id"])

        return super().delete_existing(obj, commit=commit)
    

class DeviceFirmwareInline(
    MultitenantAdminMixin, DeactivatedDeviceReadOnlyMixin, admin.StackedInline
):
    model = DeviceFirmware
    formset = DeviceFormSet
    form = DeviceFirmwareForm
    exclude = ["created"]
    select_related = ["device", "image"]
    readonly_fields = ["installed", "modified"]
    verbose_name = _("Firmware")
    verbose_name_plural = verbose_name
    extra = 0
    multitenant_shared_relations = ["device"]
    template = "admin/firmware_upgrader/device_firmware_inline.html"
    # hack for openwisp-monitoring integartion
    # TODO: remove when this issue solved:
    # https://github.com/theatlantic/django-nested-admin/issues/128#issuecomment-665833142
    sortable_options = {"disabled": True}

    def _get_conditional_queryset(self, request, obj, select_related=False):
        return bool(obj)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj=obj, **kwargs)
        if obj:
            try:
                schema = get_upgrader_schema_for_device(obj)
                formset.extra_context = json.dumps(schema, cls=DjangoJSONEncoder)
            except DeviceConnection.DoesNotExist:
                # We cannot retrieve the schema for upgrade options because this
                # device does not have any related DeviceConnection object.
                pass
        return formset


class DeviceUpgradeOperationForm(UpgradeOperationForm):
    class Meta(UpgradeOperationForm.Meta):
        pass

    def __init__(self, device, *args, **kwargs):
        self.device = device
        super().__init__(*args, **kwargs)


class DeviceUpgradeOperationInline(ReadonlyUpgradeOptionsMixin, UpgradeOperationInline):
    verbose_name = _("Recent Firmware Upgrades")
    verbose_name_plural = verbose_name
    formset = DeviceFormSet
    form = DeviceUpgradeOperationForm
    # hack for openwisp-monitoring integration
    # TODO: remove when this issue solved:
    # https://github.com/theatlantic/django-nested-admin/issues/128#issuecomment-665833142
    sortable_options = {"disabled": True}
    can_delete = True
    extra = 0

    class Media:
        js = [
            "admin/js/jquery.init.js",
            "firmware-upgrader/js/upgrade-auto-refresh.js",
        ]
        css = {"all": ["firmware-upgrader/css/upgrade-options.css"]}

    fields = [
        "device",
        "image",
        "status",
        "progress_display",
        "scheduled_at_display",
        "log",
        "readonly_upgrade_options",
        "modified",
    ]
    readonly_fields = fields

    def get_queryset(self, request, select_related=True):
        qs = super().get_queryset(request)

        resolved = resolve(request.path_info)
        if "object_id" in resolved.kwargs:
            seven_days = localtime() - timedelta(days=7)
            qs = qs.filter(
                device_id=resolved.kwargs["object_id"], created__gte=seven_days,
            ).order_by("-created")

        # ✅ Important: pull schedule in same query path
        return qs.select_related(
            "device",
            "image",
            "device__devicefirmware",
            "device__devicefirmware__schedule",
        )
        
    def has_delete_permission(self, request, obj=None):
        return True

    @admin.display(description="Scheduled at")
    def scheduled_at_display(self, obj):
        # show only for scheduled
        if obj.status != "scheduled":
            return "-"

        df = getattr(obj.device, "devicefirmware", None)
        if not df:
            return "-"
        sched = getattr(df, "schedule", None)
        if not sched or not sched.scheduled_at:
            return "-"

        scheduled_local = timezone.localtime(sched.scheduled_at)
        now_local = timezone.localtime(timezone.now())

        # remaining time
        remaining = scheduled_local - now_local

        # if already passed but still marked scheduled
        if remaining <= timedelta(seconds=0):
            remaining_txt = "0s"
        else:
            total_seconds = int(remaining.total_seconds())
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)

            if days > 0:
                remaining_txt = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                remaining_txt = f"{hours}h {minutes}m"
            elif minutes > 0:
                remaining_txt = f"{minutes}m {seconds}s"
            else:
                remaining_txt = f"{seconds}s"

        # show both
        # return f"{date_format(scheduled_local, 'DATETIME_FORMAT')} (in {remaining_txt})"
        return format_html(
        "{} <span style='color:#6b7280;'>(starts in <b>{}</b>)</span>",
        date_format(scheduled_local, "DATETIME_FORMAT"),
        remaining_txt,
    )



    def _get_conditional_queryset(self, request, obj, select_related=False):
        if obj:
            return self.get_queryset(request, select_related=False).exists()
        return False
    
    @staticmethod
    def _format_size(size_bytes):
        """Format bytes into human-readable size string."""
        if size_bytes >= 1048576:
            return f"{size_bytes / 1048576:.1f} MB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes} B"

    @admin.display(description="Progress")
    def progress_display(self, obj):
        if obj.status == "scheduled":
            return "-"
        percent = obj.progress
        # Bar color and badge per status
        if obj.status == "success":
            bar_bg = "background:linear-gradient(90deg,#22c55e,#16a34a)"
            badge_style = "background:#dcfce7;color:#166534"
            badge_text = "completed"
        elif obj.status == "in-progress":
            bar_bg = (
                "background:#16a34a;"
                "background-size:20px 20px;"
                "background-image:linear-gradient(-45deg,"
                "rgba(255,255,255,.25) 25%,transparent 25%,"
                "transparent 50%,rgba(255,255,255,.25) 50%,"
                "rgba(255,255,255,.25) 75%,transparent 75%,transparent);"
                "animation:fw-stripe .8s linear infinite"
            )
            badge_style = "background:#dcfce7;color:#166534"
            badge_text = "uploading"
        elif obj.status in ("failed", "aborted"):
            bar_bg = "background:linear-gradient(90deg,#ef4444,#dc2626)"
            badge_style = "background:#fee2e2;color:#991b1b"
            badge_text = obj.status
        else:
            bar_bg = "background:#6c757d"
            badge_style = "background:#f3f4f6;color:#6b7280"
            badge_text = obj.status
        # Use model properties for real size-based progress:
        # progress = (uploaded_bytes / firmware_size) * 100
        total_bytes = obj.firmware_size
        uploaded_bytes = obj.uploaded_bytes
        remaining_bytes = max(total_bytes - uploaded_bytes, 0)
        size_text = ""
        if total_bytes > 0:
            uploaded_str = self._format_size(uploaded_bytes)
            total_str = self._format_size(total_bytes)
            remaining_str = self._format_size(remaining_bytes)
            if obj.status == "in-progress":
                size_text = f'{uploaded_str} / {total_str} &mdash; {remaining_str} remaining'
            elif obj.status == "success":
                size_text = f'{total_str} uploaded'
            else:
                size_text = f'{uploaded_str} / {total_str}'
        # Build single horizontal row: [bar] [percent] [size] [badge]
        html = (
            f'<style>'
            f'@keyframes fw-stripe{{'
            f'from{{background-position:0 0}}'
            f'to{{background-position:20px 0}}'
            f'}}</style>'
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'  flex-wrap:nowrap;white-space:nowrap;">'
            # Progress bar track
            f'  <div style="width:400px;min-width:200px;height:18px;'
            f'    background:#d1d5db;border-radius:9px;overflow:hidden;'
            f'    box-shadow:inset 0 2px 4px rgba(0,0,0,.15);">'
            f'    <div style="height:100%;border-radius:9px;'
            f'      width:{percent}%;min-width:2px;{bar_bg};'
            f'      display:flex;align-items:center;justify-content:center;'
            f'      font-size:10px;font-weight:700;color:#fff;'
            f'      text-shadow:0 1px 1px rgba(0,0,0,.2);'
            f'      transition:width .6s cubic-bezier(.4,0,.2,1);">'
            f'      {"" if percent < 8 else str(percent) + "%"}'
            f'    </div>'
            f'  </div>'
            # Percent text (always visible outside bar)
            f'  <span style="font-size:12px;font-weight:700;'
            f'    color:#374151;min-width:36px;">{percent}%</span>'
            # Size info
            f'  <span style="font-size:11px;color:#6b7280;">'
            f'    {size_text}</span>'
            # Status badge
            f'  <span style="display:inline-block;padding:2px 8px;'
            f'    border-radius:10px;font-size:10px;font-weight:700;'
            f'    text-transform:uppercase;letter-spacing:.5px;'
            f'    {badge_style};">{badge_text}</span>'
            f'</div>'
        )
        return mark_safe(html)



# DeviceAdmin.get_inlines = device_admin_get_inlines
DeviceAdmin.conditional_inlines += [DeviceFirmwareInline, DeviceUpgradeOperationInline]

reversion.register(model=DeviceFirmware, follow=["device"])
reversion.register(model=UpgradeOperation)
DeviceAdmin.add_reversion_following(follow=["devicefirmware", "upgradeoperation_set"])
