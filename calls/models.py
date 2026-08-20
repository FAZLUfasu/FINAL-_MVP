from django.contrib.auth.models import User
from django.db import models


class Contact(models.Model):
  """Stores uploaded lead lists and customer contact info."""

  name = models.CharField(max_length=255, default="Unknown")
  phone_number = models.CharField(max_length=20, unique=True)
  uploaded_at = models.DateTimeField(auto_now_add=True)

  def __str__(self):
    return f"{self.name} ({self.phone_number})"


class CallSession(models.Model):
  """Tracks phone call audio recordings and session metadata."""

  STATUS_CHOICES = [
      ("queued", "Queued"),
      ("active", "Active"),
      ("completed", "Completed"),
      ("failed", "Failed"),
  ]
  contact = models.ForeignKey(
      Contact, on_delete=models.CASCADE, related_name="calls"
  )
  status = models.CharField(
      max_length=20, choices=STATUS_CHOICES, default="queued"
  )
  duration_seconds = models.IntegerField(default=0)

  # 🟢 Separate Audio File Fields
  recording_file = models.FileField(
      upload_to="call_recordings/",
      blank=True,
      null=True,
      help_text="Customer voice audio",
  )
  ai_recording_file = models.FileField(
      upload_to="ai_recordings/",
      blank=True,
      null=True,
      help_text="AI response audio",
  )

  created_at = models.DateTimeField(auto_now_add=True)

  def __str__(self):
    return f"Call to {self.contact.phone_number} - {self.status}"

class SalesInsight(models.Model):
  """Stores post-call AI extraction payloads."""

  call_session = models.OneToOneField(
      CallSession, on_delete=models.CASCADE, related_name="insight"
  )
  extracted_data = models.JSONField(
      help_text=(
          "Stores: customer_name, primary_intent, most_asked_question,"
          " ai_failure_point"
      )
  )
  needs_followup = models.BooleanField(default=False)
  processed_at = models.DateTimeField(auto_now_add=True)

  def __str__(self):
    return f"AI Insights for Call #{self.call_session.id}"


class CallQueueItem(models.Model):
  """Manages outgoing call queue tasks and aggregated stats."""

  STATUS_CHOICES = [
      ("PENDING", "Pending"),
      ("CALLED", "Called"),
      ("FOLLOW_UP", "Follow Up"),
  ]

  name = models.CharField(max_length=255)
  phone_number = models.CharField(max_length=20)
  details = models.TextField(
      blank=True, null=True, help_text="Context or objective for AI Co-Pilot"
  )
  status = models.CharField(
      max_length=20, choices=STATUS_CHOICES, default="PENDING"
  )

  call_duration_seconds = models.IntegerField(default=0)
  ai_summary = models.TextField(blank=True, null=True)
  top_question = models.CharField(max_length=255, blank=True, null=True)

  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  def __str__(self):
    return f"{self.name} ({self.phone_number}) - {self.status}"


class CompanyScript(models.Model):
  """Configures system prompts and AI personality instructions."""

  company_name = models.CharField(
      max_length=100, default="Brainex AI Solution"
  )
  bot_name = models.CharField(max_length=50, default="Alex")

  opening_greeting = models.TextField(
      default=(
          "Hello! This is Alex from Brainex AI. How can I assist you today?"
      )
  )
  closing_statement = models.TextField(
      default=(
          "Thank you for contacting Brainex AI. Have a great day ahead!"
          " Goodbye."
      )
  )

  company_details = models.TextField(
      help_text="Enter pricing, services, working hours, FAQs, etc."
  )

  is_active = models.BooleanField(
      default=True, help_text="Set as the active voice prompt"
  )
  updated_at = models.DateTimeField(auto_now=True)

  def __str__(self):
    return (
        f"{self.company_name} Script"
        f" ({'Active' if self.is_active else 'Inactive'})"
    )