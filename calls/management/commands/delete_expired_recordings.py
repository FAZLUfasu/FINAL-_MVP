from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from ...models import CallSession, SystemSettings


class Command(BaseCommand):
    help = "Delete expired customer and AI call recordings."

    def handle(self, *args, **options):

        # ============================================================
        # LOAD TELICALL SETTINGS
        # ============================================================

        settings_obj = SystemSettings.get_settings()

        if not settings_obj.automatic_recording_cleanup:
            self.stdout.write(
                self.style.WARNING(
                    "Automatic recording cleanup is disabled."
                )
            )
            return

        retention_days = settings_obj.recording_retention_days

        cutoff_time = timezone.now() - timedelta(
            days=retention_days
        )

        self.stdout.write("")
        self.stdout.write(
            "=============================================="
        )
        self.stdout.write(
            "TELICALL AUTOMATIC RECORDING CLEANUP"
        )
        self.stdout.write(
            "=============================================="
        )

        self.stdout.write(
            f"Retention period : {retention_days} days"
        )

        self.stdout.write(
            f"Delete before    : {cutoff_time}"
        )

        # ============================================================
        # FIND EXPIRED CALL SESSIONS
        # ============================================================

        old_sessions = (
            CallSession.objects
            .filter(
                created_at__lt=cutoff_time
            )
            .filter(
                Q(recording_file__isnull=False)
                | Q(ai_recording_file__isnull=False)
            )
            .order_by("created_at")
        )

        customer_deleted = 0
        ai_deleted = 0
        sessions_updated = 0
        errors = 0

        total_sessions = old_sessions.count()

        self.stdout.write(
            f"Expired sessions : {total_sessions}"
        )

        self.stdout.write("")

        # ============================================================
        # PROCESS EACH CALL SESSION
        # ============================================================

        for session in old_sessions:

            changed_fields = []

            self.stdout.write(
                f"Processing CallSession #{session.id}"
            )

            # ========================================================
            # DELETE CUSTOMER RECORDING
            # ========================================================

            if session.recording_file:

                try:

                    filename = session.recording_file.name

                    session.recording_file.delete(
                        save=False
                    )

                    session.recording_file = None

                    changed_fields.append(
                        "recording_file"
                    )

                    customer_deleted += 1

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Customer audio deleted: {filename}"
                        )
                    )

                except Exception as exc:

                    errors += 1

                    self.stderr.write(
                        self.style.ERROR(
                            f"  Customer audio delete failed "
                            f"for CallSession #{session.id}: {exc}"
                        )
                    )

            # ========================================================
            # DELETE AI RESPONSE RECORDING
            # ========================================================

            if session.ai_recording_file:

                try:

                    filename = session.ai_recording_file.name

                    session.ai_recording_file.delete(
                        save=False
                    )

                    session.ai_recording_file = None

                    changed_fields.append(
                        "ai_recording_file"
                    )

                    ai_deleted += 1

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  AI audio deleted: {filename}"
                        )
                    )

                except Exception as exc:

                    errors += 1

                    self.stderr.write(
                        self.style.ERROR(
                            f"  AI recording delete failed "
                            f"for CallSession #{session.id}: {exc}"
                        )
                    )

            # ========================================================
            # UPDATE DATABASE
            #
            # IMPORTANT:
            # CallSession itself is NOT deleted.
            # Only audio FileField values are cleared.
            # ========================================================

            if changed_fields:

                try:

                    session.save(
                        update_fields=changed_fields
                    )

                    sessions_updated += 1

                except Exception as exc:

                    errors += 1

                    self.stderr.write(
                        self.style.ERROR(
                            f"  Database update failed "
                            f"for CallSession #{session.id}: {exc}"
                        )
                    )

        # ============================================================
        # CLEANUP SUMMARY
        # ============================================================

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "=============================================="
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "TELICALL RECORDING CLEANUP COMPLETE"
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                "=============================================="
            )
        )

        self.stdout.write(
            f"Retention period       : {retention_days} days"
        )

        self.stdout.write(
            f"Expired sessions found : {total_sessions}"
        )

        self.stdout.write(
            f"Sessions updated       : {sessions_updated}"
        )

        self.stdout.write(
            f"Customer audio deleted : {customer_deleted}"
        )

        self.stdout.write(
            f"AI audio deleted       : {ai_deleted}"
        )

        self.stdout.write(
            f"Errors                 : {errors}"
        )

        self.stdout.write(
            self.style.SUCCESS(
                "CallSession records    : KEPT"
            )
        )

        self.stdout.write("")