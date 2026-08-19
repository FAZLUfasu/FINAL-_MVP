from django.db import models
from django.contrib.auth.models import User


class CallQueueItem(models.Model):
    name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=20)
    details = models.TextField(help_text="Context or objective for AI Co-Pilot")
    status = models.CharField(max_length=20, default="PENDING")  # PENDING, COMPLETED, FAILED
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.phone_number}"

class Contact(models.Model):
    """Managed by 'contacts' app: Stores your uploaded lead lists"""
    name = models.CharField(max_length=255, default="Unknown")
    phone_number = models.CharField(max_length=20, unique=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} ({self.phone_number})"

class CallSession(models.Model):
    """Managed by 'calls' app: Tracks every active phone connection"""
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="calls")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    duration_seconds = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Call to {self.contact.phone_number} - {self.status}"

class SalesInsight(models.Model):
    """Managed by 'analytics' app: Stores the rich JSON post-call payload from Llama"""
    call_session = models.OneToOneField(CallSession, on_delete=models.CASCADE, related_name="insight")
    
    # 🔥 The PostgreSQL Advantage: Native, fast, searchable JSON storage
    extracted_data = models.JSONField(help_text="Stores: customer_name, primary_intent, most_asked_question, ai_failure_point")
    
    needs_followup = models.BooleanField(default=False)
    processed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"AI Insights for Call #{self.call_session.id}"
# calls/models.py
from django.db import models

class CallQueueItem(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('CALLED', 'Called'),
        ('FOLLOW_UP', 'Follow Up'),
    ]

    name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20)
    details = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    
    # Call Summary & Analytics
    call_duration_seconds = models.IntegerField(default=0)
    ai_summary = models.TextField(blank=True, null=True)
    top_question = models.CharField(max_length=255, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.phone_number}) - {self.status}"
    

class CompanyScript(models.Model):
    company_name = models.CharField(max_length=100, default="Brainex AI Solution")
    bot_name = models.CharField(max_length=50, default="Alex")
    
    # Custom Greetings & Closings
    opening_greeting = models.TextField(
        default="Hello! This is Alex from Brainex AI. How can I assist you today?"
    )
    closing_statement = models.TextField(
        default="Thank you for contacting Brainex AI. Have a great day ahead! Goodbye."
    )
    
    # Knowledge Base / Company Info
    company_details = models.TextField(
        help_text="Enter pricing, services, working hours, FAQs, etc."
    )
    
    is_active = models.BooleanField(default=True, help_text="Set as the active voice prompt")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.company_name} Script ({'Active' if self.is_active else 'Inactive'})"