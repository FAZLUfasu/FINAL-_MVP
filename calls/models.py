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
  """
  Structured company + AI telecalling configuration.

  Current use:
      Brainex AI Institute

  Future use:
      This structure can later be moved under Company -> TelicallLine
      without changing the core call engine.
  """

  company_name = models.CharField(
      max_length=150,
      default="Brainex AI Institute",
  )

  bot_name = models.CharField(
      max_length=100,
      default="Brainex AI Assistant",
      help_text="Name used by the AI caller.",
  )

  tagline = models.CharField(
      max_length=200,
      blank=True,
      default="Plug into Practical AI.",
  )

  company_description = models.TextField(
      blank=True,
      help_text="Short description of the company/institute.",
  )

  target_audience = models.TextField(
      blank=True,
      help_text=(
          "Who the service is for. Example: students, working "
          "professionals, business owners, educators, creators, parents."
      ),
  )

  products_services = models.TextField(
      blank=True,
      help_text=(
          "Courses, programs, products or services the AI is allowed "
          "to discuss."
      ),
  )

  key_benefits = models.TextField(
      blank=True,
      help_text="Main benefits/use cases the AI can explain to customers.",
  )

  course_durations = models.TextField(
      blank=True,
      help_text=(
          "Available durations. Example: 5 days, 10 days, 15 days, "
          "1 month, 6 months."
      ),
  )

  pricing_information = models.TextField(
      blank=True,
      help_text=(
          "Only enter verified pricing here. Leave blank if the AI "
          "must refer pricing questions to a human."
      ),
  )

  working_hours = models.CharField(
      max_length=255,
      blank=True,
      help_text="Optional company/team working hours.",
  )

  contact_information = models.TextField(
      blank=True,
      help_text="Phone, WhatsApp, email or other official contact information.",
  )

  frequently_asked_questions = models.TextField(
      blank=True,
      help_text="Verified FAQs and answers the AI is allowed to use.",
  )

  sales_objective = models.TextField(
      blank=True,
      default=(
          "Understand the customer's interest in practical AI training, "
          "identify their profile and need, explain the relevant offering, "
          "and classify whether they are interested, need more information, "
          "are ready to join, or want a callback."
      ),
      help_text="Main objective of this telecalling campaign.",
  )

  data_to_collect = models.TextField(
      blank=True,
      default=(
          "Customer type, education or occupation when relevant, "
          "interest area, course interest, level of interest, "
          "and callback requirement."
      ),
      help_text="Information the AI should try to collect naturally.",
  )

  ai_rules = models.TextField(
      blank=True,
      default=(
          "Keep responses short and natural for a phone call. "
          "Ask one main question at a time. "
          "Do not repeat already collected information. "
          "Do not invent fees, schedules, discounts, certificates, "
          "guarantees or other facts. "
          "If exact information is unavailable, offer human follow-up. "
          "Respect refusal immediately and never pressure the customer."
      ),
      help_text="Behavioral rules the AI must follow.",
  )

  opening_greeting = models.TextField(
      default=(
          "Hello, I'm the AI calling assistant from Brainex AI Institute. "
          "We provide practical AI training programs. "
          "May I briefly tell you about them?"
      )
  )

  closing_statement = models.TextField(
      default=(
          "Thank you for your time. If you need more information, "
          "our Brainex team can contact you and assist you further."
      )
  )

  company_details = models.TextField(
      blank=True,
      help_text=(
          "Legacy / combined company information used by older "
          "consumer versions."
      ),
  )

  is_active = models.BooleanField(
      default=True,
      help_text="Use this configuration for the current AI caller.",
  )

  updated_at = models.DateTimeField(auto_now=True)

  def build_ai_knowledge(self):
      sections = []

      if self.company_description:
          sections.append(f"COMPANY DESCRIPTION:\n{self.company_description}")
      if self.tagline:
          sections.append(f"TAGLINE:\n{self.tagline}")
      if self.target_audience:
          sections.append(f"TARGET AUDIENCE:\n{self.target_audience}")
      if self.products_services:
          sections.append(f"PRODUCTS / SERVICES:\n{self.products_services}")
      if self.key_benefits:
          sections.append(f"KEY BENEFITS:\n{self.key_benefits}")
      if self.course_durations:
          sections.append(f"COURSE / PROGRAM DURATIONS:\n{self.course_durations}")
      if self.pricing_information:
          sections.append(f"PRICING INFORMATION:\n{self.pricing_information}")
      if self.working_hours:
          sections.append(f"WORKING HOURS:\n{self.working_hours}")
      if self.contact_information:
          sections.append(f"CONTACT INFORMATION:\n{self.contact_information}")
      if self.frequently_asked_questions:
          sections.append(
              f"FREQUENTLY ASKED QUESTIONS:\n{self.frequently_asked_questions}"
          )
      if self.sales_objective:
          sections.append(f"SALES OBJECTIVE:\n{self.sales_objective}")
      if self.data_to_collect:
          sections.append(f"DATA TO COLLECT:\n{self.data_to_collect}")
      if self.ai_rules:
          sections.append(f"AI RULES:\n{self.ai_rules}")
      if self.company_details:
          sections.append(f"ADDITIONAL COMPANY DETAILS:\n{self.company_details}")

      return "\n\n".join(sections).strip()

  def __str__(self):
      return (
          f"{self.company_name} Script"
          f" ({'Active' if self.is_active else 'Inactive'})"
      )
class CustomVoice(models.Model):
    """Stores reference recordings for a future cloned Telicall AI voice."""

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("ready", "Ready"),
        ("disabled", "Disabled"),
    ]

    LANGUAGE_CHOICES = [
        ("English", "English"),
        ("Malayalam", "Malayalam"),
        ("Hindi", "Hindi"),
        ("Tamil", "Tamil"),
        ("Other", "Other"),
    ]

    name = models.CharField(
        max_length=150,
        unique=True,
        help_text="Example: Brainex Female 1",
    )

    language = models.CharField(
        max_length=50,
        choices=LANGUAGE_CHOICES,
        default="English",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
    )

    description = models.TextField(
        blank=True,
        help_text="Optional notes about this voice.",
    )

    sample_1 = models.FileField(
        upload_to="custom_voices/",
        blank=True,
        null=True,
        help_text="Upload WAV or record directly in the browser.",
    )

    sample_2 = models.FileField(
        upload_to="custom_voices/",
        blank=True,
        null=True,
        help_text="Optional additional voice sample.",
    )

    sample_3 = models.FileField(
        upload_to="custom_voices/",
        blank=True,
        null=True,
        help_text="Optional additional voice sample.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return self.name
    
class SystemSettings(models.Model):
    """Global Telicall system settings."""

    RETENTION_CHOICES = [
        (1, "1 Day"),
        (3, "3 Days"),
        (5, "5 Days"),
        (7, "7 Days"),
        (15, "15 Days"),
        (30, "30 Days"),
        (60, "60 Days"),
        (90, "90 Days"),
    ]

    TTS_ENGINE_CHOICES = [
        ("sapi", "Windows SAPI"),
        ("custom", "Custom Voice"),
    ]

    SAPI_VOICE_CHOICES = [
        ("Microsoft David Desktop", "Microsoft David Desktop - Male"),
        ("Microsoft Zira Desktop", "Microsoft Zira Desktop - Female"),
    ]

    recording_retention_days = models.PositiveIntegerField(
        choices=RETENTION_CHOICES,
        default=5,
        help_text=(
            "Customer and AI response recordings older than "
            "this period will automatically be deleted."
        ),
    )

    automatic_recording_cleanup = models.BooleanField(
        default=True,
        help_text=(
            "Enable or disable automatic deletion of expired "
            "customer and AI call recordings."
        ),
    )

    # ========================================================
    # AI VOICE SETTINGS
    # ========================================================

    tts_engine = models.CharField(
        max_length=20,
        choices=TTS_ENGINE_CHOICES,
        default="sapi",
        help_text="Select Windows SAPI or a cloned custom voice.",
    )

    use_system_default_voice = models.BooleanField(
        default=False,
        help_text="Use the Windows default voice instead of selecting one.",
    )

    tts_voice_name = models.CharField(
        max_length=255,
        choices=SAPI_VOICE_CHOICES,
        default="Microsoft Zira Desktop",
        help_text="Installed Windows SAPI voice.",
    )

    custom_voice = models.ForeignKey(
        "CustomVoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="system_settings",
        help_text="Select the custom cloned voice when Voice Engine is Custom Voice.",
    )

    tts_rate = models.IntegerField(
        default=-1,
        help_text="Windows SAPI speech rate from -10 to +10.",
    )

    tts_volume = models.PositiveIntegerField(
        default=95,
        help_text="Speech volume from 0 to 100.",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "recording_retention_days": 5,
                "automatic_recording_cleanup": True,
                "tts_engine": "sapi",
                "use_system_default_voice": False,
                "tts_voice_name": "Microsoft Zira Desktop",
                "tts_rate": -1,
                "tts_volume": 95,
            },
        )
        return obj

    def __str__(self):
        return "Telicall System Settings"