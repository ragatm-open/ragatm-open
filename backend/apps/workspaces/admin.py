from django.contrib import admin
from .models import Workspace, Membership

class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ["user"]
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)

@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "created_at")
    search_fields = ("name", "id")
    readonly_fields = ("created_at",)
    inlines = [MembershipInline]

@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "user", "role", "created_at")
    list_filter = ("role", "workspace")
    search_fields = ("workspace__name", "user__email", "workspace__id", "user__id")
    autocomplete_fields = ("workspace", "user")
    readonly_fields = ("created_at",)
