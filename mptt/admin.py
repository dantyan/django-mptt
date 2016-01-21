from __future__ import unicode_literals

import json

from django import http
from django.conf import settings
from django.contrib.admin.actions import delete_selected
from django.contrib.admin.options import ModelAdmin
from django.utils.encoding import force_text
from django.utils.html import format_html
from django.utils.translation import ugettext as _

from mptt.exceptions import InvalidMove
from mptt.forms import MPTTAdminForm, TreeNodeChoiceField
from mptt.models import MPTTModel, TreeForeignKey

__all__ = ('MPTTModelAdmin', 'MPTTAdminForm')
IS_GRAPPELLI_INSTALLED = 'grappelli' in settings.INSTALLED_APPS


class MPTTModelAdmin(ModelAdmin):
    """
    A basic admin class that displays tree items according to their position in
    the tree.  No extra editing functionality beyond what Django admin normally
    offers.
    """

    if IS_GRAPPELLI_INSTALLED:
        change_list_template = 'admin/grappelli_mptt_change_list.html'
    else:
        change_list_template = 'admin/mptt_change_list.html'

    form = MPTTAdminForm

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if issubclass(db_field.rel.to, MPTTModel) \
                and not isinstance(db_field, TreeForeignKey) \
                and db_field.name not in self.raw_id_fields:
            db = kwargs.get('using')

            limit_choices_to = db_field.get_limit_choices_to()
            defaults = dict(
                form_class=TreeNodeChoiceField,
                queryset=db_field.rel.to._default_manager.using(
                    db).complex_filter(limit_choices_to),
                required=False)
            defaults.update(kwargs)
            kwargs = defaults
        return super(MPTTModelAdmin, self).formfield_for_foreignkey(
            db_field, request, **kwargs)

    def get_ordering(self, request):
        """
        Changes the default ordering for changelists to tree-order.
        """
        mptt_opts = self.model._mptt_meta
        return self.ordering or (mptt_opts.tree_id_attr, mptt_opts.left_attr)

    def delete_selected_tree(self, modeladmin, request, queryset):
        """
        Deletes multiple instances and makes sure the MPTT fields get
        recalculated properly. (Because merely doing a bulk delete doesn't
        trigger the post_delete hooks.)
        """
        # If this is True, the confirmation page has been displayed
        if request.POST.get('post'):
            n = 0
            with queryset.model._tree_manager.delay_mptt_updates():
                for obj in queryset:
                    if self.has_delete_permission(request, obj):
                        obj.delete()
                        n += 1
                        obj_display = force_text(obj)
                        self.log_deletion(request, obj, obj_display)
            self.message_user(
                request,
                _('Successfully deleted %(count)d items.') % {'count': n})
            # Return None to display the change list page again
            return None
        else:
            # (ab)using the built-in action to display the confirmation page
            return delete_selected(self, request, queryset)

    def get_actions(self, request):
        actions = super(MPTTModelAdmin, self).get_actions(request)
        if 'delete_selected' in actions:
            actions['delete_selected'] = (
                self.delete_selected_tree,
                'delete_selected',
                _('Delete selected %(verbose_name_plural)s'))
        return actions


class DraggableMPTTAdmin(MPTTModelAdmin):
    """
    The ``DraggableMPTTAdmin`` modifies the standard Django administration
    change list to a drag-drop enabled interface.
    """

    list_per_page = 2000  # This will take a really long time to load.

    def __init__(self, *args, **kwargs):
        super(DraggableMPTTAdmin, self).__init__(*args, **kwargs)

        opts = self.model._meta
        self.change_list_template = [
            'admin/%s/%s/draggable_mptt_change_list.html' % (
                opts.app_label, opts.object_name.lower()),
            'admin/%s/draggable_mptt_change_list.html' % opts.app_label,
            'admin/draggable_mptt_change_list.html',
        ]

    def tree_actions(self, item):
        try:
            url = item.get_absolute_url()
        except Exception:  # Nevermind.
            url = ''

        return format_html(
            '<div class="drag_handle"></div>'
            '<div id="tree_marker-{}" class="tree_marker"'
            ' data-url="{}"></div>',
            item.pk,
            url,
        )
    tree_actions.short_description = ''

    def indented_title(self, item):
        """
        Generate a short title for an object, indent it depending on
        the object's depth in the hierarchy.
        """
        return format_html(
            '<div style="text-indent:{}px">{}</div>',
            item._mpttfield('level') * 20,
            item,
        )
    indented_title.short_description = _('title')

    def changelist_view(self, request, extra_context=None, *args, **kwargs):
        """
        Handle the changelist view, the django view for the model instances
        change list/actions page.
        """
        # handle common AJAX requests
        if request.is_ajax():
            cmd = request.POST.get('cmd')
            if cmd == 'move_node':
                return self._move_node(request)
            return http.HttpResponseBadRequest(
                'Oops. AJAX request not understood.')

        extra_context = extra_context or {}
        extra_context['draggable_mptt_admin_context'] = DraggableMPTTAdminContext(
            self.get_queryset(request))

        return super(DraggableMPTTAdmin, self).changelist_view(
            request, extra_context, *args, **kwargs)

    def _move_node(self, request):
        position = request.POST.get('position')
        if position not in ('last-child', 'left', 'right'):
            self.message_user(request, _('Did not understand moving instruction.'))
            return http.HttpResponse('FAIL, unknown instruction.')

        queryset = self.get_queryset(request)
        try:
            cut_item = queryset.get(pk=request.POST.get('cut_item'))
            pasted_on = queryset.get(pk=request.POST.get('pasted_on'))
        except (self.model.DoesNotExist, TypeError, ValueError):
            self.message_user(request, _('Objects have disappeared, try again.'))
            return http.HttpResponse('FAIL, invalid objects.')

        if not self.has_change_permission(request, cut_item):
            self.message_user(request, _('No permission'))
            return http.HttpResponse('FAIL, no permission.')

        try:
            self.model._tree_manager.move_node(cut_item, pasted_on, position)
        except InvalidMove as e:
            self.message_user(request, '%s' % e)
            return http.HttpResponse('FAIL, invalid move.')

        self.message_user(
            request,
            _('%s has been successfully moved.') % cut_item)
        return http.HttpResponse('OK, moved.')


class DraggableMPTTAdminContext(object):
    """
    Helper object for adding all required data for the draggable mptt admin
    Javascript code
    """
    def __init__(self, queryset):
        self.queryset = queryset
        self.model = queryset.model

    def context(self):
        opts = self.model._meta

        return json.dumps({
            'cookieName': 'tree_%s_%s_collapsed' % (opts.app_label, opts.model_name),
            'treeStructure': self.build_tree_structure(),
            'nodeLevels': dict(
                self.queryset.values_list(
                    'pk',
                    self.model._mptt_meta.level_attr,
                )
            ),
        })

    def build_tree_structure(self):
        """
        Build an in-memory representation of the item tree, trying to keep
        database accesses down to a minimum. The returned dictionary looks like
        this (as json dump):

            {"6": [7, 8, 10]
             "7": [12],
             ...
             }

        Leaves are not included in the dictionary.
        """
        all_nodes = {}

        mptt_opts = self.model._mptt_meta
        items = self.queryset.values_list(
            'pk',
            '%s_id' % mptt_opts.parent_attr,
        )
        for p_id, parent_id in items:
            all_nodes.setdefault(
                str(parent_id) if parent_id else 0,
                [],
            ).append(p_id)
        return all_nodes
