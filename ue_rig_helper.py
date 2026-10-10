bl_info = {
    "name": "UE Rig Helper",
    "author": "Codex",
    "version": (1, 3, 0),
    "blender": (4, 4, 0),
    "location": "3D View > Sidebar > UE Rig Helper",
    "description": "Control rigs and animation export for Unreal Engine",
    "category": "Import-Export",
}

import math
import ast
from contextlib import contextmanager
import os
import re
import shutil
import tempfile

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix, Vector


def set_enum(owner, prop, value):
    identifiers = {item.identifier for item in owner.bl_rna.properties[prop].enum_items}
    if value not in identifiers:
        raise RuntimeError(f"Unsupported {prop}: {value}")
    setattr(owner, prop, value)


def object_mode(value):
    identifiers = {item.identifier for item in bpy.ops.object.mode_set.get_rna_type().properties["mode"].enum_items}
    if value not in identifiers:
        raise RuntimeError(f"Unsupported mode: {value}")
    bpy.ops.object.mode_set(mode=value)


def rigs(context):
    settings = context.scene.ue4_export_settings
    rig = settings.control_rig or context.scene.objects.get("UE4_ControlRig")
    if rig is None or rig.type != "ARMATURE":
        raise RuntimeError("Choose the control rig")
    source = settings.source_rig or context.scene.objects.get(rig.get("source_skeleton", "root"))
    if source is None or source.type != "ARMATURE" or source == rig:
        raise RuntimeError("Choose the original UE4 skeleton")
    if source.name not in context.view_layer.objects:
        raise RuntimeError("The original skeleton is excluded from this view layer")
    return rig, source


def clip_range(scene, rig, action=None):
    action = action or (rig.animation_data.action if rig.animation_data else None)
    if scene.ue4_export_settings.frame_range == "ACTION" and action:
        start, end = action.frame_range
        return math.floor(start), math.ceil(end)
    return scene.frame_start, scene.frame_end


def filename_safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Animation"


def slot_curves(action, slot):
    return [curve for layer in action.layers for strip in layer.strips
            for bag in strip.channelbags if bag.slot_handle == slot.handle for curve in bag.fcurves]


def compatible_slots(action, rig):
    """Only offer animation belonging to this control rig, rather than meshes or imported deform rigs."""
    if action is None or rig is None or rig.type != "ARMATURE":
        return []
    referenced = False
    if rig.animation_data:
        referenced = rig.animation_data.action == action or any(
            strip.action == action for track in rig.animation_data.nla_tracks for strip in track.strips)
    compatible = []
    for slot in action.slots:
        if slot.target_id_type != rig.id_type:
            continue
        curves = slot_curves(action, slot)
        if not curves:
            continue
        has_bones = False
        valid = True
        for curve in curves:
            path = curve.data_path
            match = re.match(r'^pose\.bones\[(.+?)\]', path)
            if match:
                has_bones = True
                try:
                    name = ast.literal_eval(match.group(1))
                except (SyntaxError, ValueError):
                    valid = False
                    break
                if name not in rig.pose.bones:
                    valid = False
                    break
            try:
                rig.path_resolve(path)
            except (ValueError, AttributeError):
                valid = False
                break
        if valid and (has_bones or referenced or slot.identifier[2:] == rig.name):
            compatible.append(slot)
    return compatible


def choose_slot(action, rig, identifier="_AUTO"):
    slots = compatible_slots(action, rig)
    if identifier and identifier != "_AUTO":
        slot = next((slot for slot in slots if slot.identifier == identifier), None)
        if slot is None:
            raise RuntimeError(f"{action.name}: the selected Action slot is not compatible with this rig")
        return slot
    exact = next((slot for slot in slots if slot.identifier[2:] == rig.name), None)
    if exact:
        return exact
    if len(slots) == 1:
        return slots[0]
    if not slots:
        raise RuntimeError(f"{action.name}: no compatible keyframes for {rig.name}")
    raise RuntimeError(f"{action.name}: choose an Action slot; several slots match this rig")


def configured_rig(settings, context=None):
    scene = (context or bpy.context).scene
    return settings.control_rig or scene.objects.get("UE4_ControlRig")


def action_poll(self, action):
    return bool(compatible_slots(action, configured_rig(self)))


def selected_action(settings, rig):
    return settings.action or (rig.animation_data.action if rig.animation_data else None)


_slot_item_cache = {}


def slot_items_for(action, rig, owner_key):
    items = [("_AUTO", "Auto", "Use the slot belonging to this rig, or its only compatible slot")]
    if action:
        items.extend((slot.identifier, slot.name_display, slot.identifier) for slot in compatible_slots(action, rig))
    # Blender keeps references to dynamic enum strings, so retain the whole item list.
    _slot_item_cache[owner_key] = items
    return items


def selected_slot_items(self, context):
    rig = configured_rig(self, context)
    return slot_items_for(selected_action(self, rig), rig, ("single", self.as_pointer()))


def batch_slot_items(self, context):
    return slot_items_for(self.action, configured_rig(context.scene.ue4_export_settings, context),
                          ("batch", self.as_pointer()))


def refresh_actions(context):
    settings = context.scene.ue4_export_settings
    rig, _ = rigs(context)
    existing = {entry.action.as_pointer(): (entry.enabled, entry.slot_identifier)
                for entry in settings.batch_actions if entry.action}
    settings.batch_actions.clear()
    for action in sorted(bpy.data.actions, key=lambda item: item.name.casefold()):
        if not compatible_slots(action, rig):
            continue
        entry = settings.batch_actions.add()
        entry.action = action
        entry.name = action.name
        entry.enabled, slot = existing.get(action.as_pointer(), (True, "_AUTO"))
        if slot == "_AUTO" or any(s.identifier == slot for s in compatible_slots(action, rig)):
            entry.slot_identifier = slot
    settings.batch_index = min(settings.batch_index, max(0, len(settings.batch_actions) - 1))


@contextmanager
def activate_action(context, rig, action, slot):
    """Keep exported clips independent, and restore the animator's live state even on failure."""
    scene = context.scene
    had_animation_data = rig.animation_data is not None
    ad = rig.animation_data_create()
    previous_action, previous_slot, previous_nla = ad.action, ad.action_slot, ad.use_nla
    previous_frame, previous_subframe = scene.frame_current, scene.frame_subframe
    previous_matrix = rig.matrix_basis.copy()
    previous_pose = {bone.name: bone.matrix_basis.copy() for bone in rig.pose.bones}
    previous_autokey = scene.tool_settings.use_keyframe_insert_auto
    try:
        scene.tool_settings.use_keyframe_insert_auto = False
        ad.action = action
        ad.action_slot = slot
        ad.use_nla = False
        # Unkeyed bones must not inherit the previously previewed clip's pose.
        for bone in rig.pose.bones:
            bone.matrix_basis = Matrix.Identity(4)
        rig.update_tag()
        scene.frame_set(previous_frame, subframe=previous_subframe)
        context.view_layer.update()
        yield
    finally:
        ad.action = previous_action
        if previous_action and previous_slot:
            ad.action_slot = previous_slot
        ad.use_nla = previous_nla
        scene.frame_set(previous_frame, subframe=previous_subframe)
        rig.matrix_basis = previous_matrix
        for name, matrix in previous_pose.items():
            rig.pose.bones[name].matrix_basis = matrix
        if not had_animation_data:
            rig.animation_data_clear()
        rig.update_tag()
        context.view_layer.update()
        scene.tool_settings.use_keyframe_insert_auto = previous_autokey


def export_fbx(context, filepath, *, character=False, action=None, slot_identifier=None,
               frame_start=None, frame_end=None, root_motion=None):
    rig, _ = rigs(context)
    kwargs = dict(character=character, frame_start=frame_start, frame_end=frame_end, root_motion=root_motion)
    if character:
        return _export_current_fbx(context, filepath, **kwargs)
    settings = context.scene.ue4_export_settings
    chosen = action or selected_action(settings, rig)
    if chosen is None:
        raise RuntimeError("Choose an Action with keyframes")
    identifier = slot_identifier if slot_identifier is not None else (settings.slot_identifier if action is None else "_AUTO")
    slot = choose_slot(chosen, rig, identifier)
    with activate_action(context, rig, chosen, slot):
        result = _export_current_fbx(context, filepath, **kwargs)
        result.update(action=chosen.name, slot=slot.identifier)
        return result


def export_batch(context, entries=None, directory=None):
    settings = context.scene.ue4_export_settings
    rig, _ = rigs(context)
    entries = list(entries) if entries is not None else [entry for entry in settings.batch_actions if entry.enabled]
    if not entries:
        raise RuntimeError("Check at least one Action")
    directory = directory or settings.directory
    if not bpy.data.filepath and directory.startswith("//"):
        raise RuntimeError("Save the .blend first, or choose an absolute export folder")
    directory = os.path.abspath(bpy.path.abspath(directory))
    used_names = set()
    plan = []
    # Check every Action and slot before writing any files.
    for entry in entries:
        action = entry.action
        if action is None:
            raise RuntimeError("An Action was deleted; refresh the list")
        slot = choose_slot(action, rig, entry.slot_identifier)
        stem = filename_safe(action.name)
        name = stem
        suffix = 2
        while name.casefold() in used_names:
            name = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(name.casefold())
        plan.append((action, slot.identifier, os.path.join(directory, name + ".fbx")))
    results, errors = [], []
    wm = context.window_manager
    wm.progress_begin(0, len(plan))
    try:
        for index, (action, slot, path) in enumerate(plan):
            try:
                # Batch always uses each clip's own Action range, even if single export uses Timeline.
                start, end = action.frame_range
                results.append(export_fbx(context, path, action=action, slot_identifier=slot,
                                          frame_start=math.floor(start), frame_end=math.ceil(end)))
            except Exception as exc:
                errors.append({"action": action.name, "error": str(exc)})
            wm.progress_update(index + 1)
    finally:
        wm.progress_end()
    return {"files": results, "errors": errors, "directory": directory}


def _export_current_fbx(context, filepath, *, character=False, frame_start=None, frame_end=None, root_motion=None):
    """Export evaluated constraints; restore pose, actions, visibility, selection and timeline even on error."""
    rig, source = rigs(context)
    scene = context.scene
    settings = scene.ue4_export_settings
    start, end = clip_range(scene, rig)
    start = start if frame_start is None else int(frame_start)
    end = end if frame_end is None else int(frame_end)
    if start > end:
        raise RuntimeError("Start frame must not exceed end frame")
    held_pose = not character and start == end
    if held_pose:
        # Unreal Interchange requires RangeStopSecond > RangeStartSecond.
        # A one-key pose remains unchanged, but gets a positive-duration take.
        end = start + 1
    do_root_motion = not character and (settings.root_motion if root_motion is None else root_motion)
    if do_root_motion and "CTRL_master" not in rig.pose.bones:
        raise RuntimeError("CTRL_master is required for root motion")
    if source.constraints:
        raise RuntimeError("The original armature object has constraints; its root transform needs a custom export setup")
    meshes = [obj for obj in scene.objects if obj.type == "MESH" and any(
        mod.type == "ARMATURE" and mod.object == source for mod in obj.modifiers)] if character else []
    if character and not meshes:
        raise RuntimeError("No meshes use the original skeleton")
    if not bpy.data.filepath and filepath.startswith("//"):
        raise RuntimeError("Save the .blend first, or choose an absolute export path")
    filepath = os.path.abspath(bpy.path.abspath(filepath))
    if not filepath.lower().endswith(".fbx"):
        filepath += ".fbx"
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    snapshot = {
        "active": context.view_layer.objects.active,
        "mode": context.object.mode if context.object else "OBJECT",
        "frame": scene.frame_current, "subframe": scene.frame_subframe,
        "range": (scene.frame_start, scene.frame_end),
        "autokey": scene.tool_settings.use_keyframe_insert_auto,
        "objects": [(o, o.hide_get(), o.hide_select, o.select_get(), o.matrix_basis.copy())
                    for o in context.view_layer.objects],
        "poses": {o: {p.name: p.matrix_basis.copy() for p in o.pose.bones} for o in (rig, source)},
        "source_rotation_mode": source.rotation_mode,
        "source_pose_position": source.data.pose_position,
        "had_animation_data": source.animation_data is not None,
        "action": source.animation_data.action if source.animation_data else None,
        "slot": source.animation_data.action_slot if source.animation_data else None,
    }
    export_root_name = rig.get("ue4_export_root_name")
    name_collision = bpy.data.objects.get(export_root_name) if export_root_name else None
    renamed_objects = []
    temporary_action = None
    temporary_path = None
    try:
        scene.tool_settings.use_keyframe_insert_auto = False
        if context.object and context.object.mode != "OBJECT":
            object_mode("OBJECT")
        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in [source] + meshes:
            obj.hide_set(False)
            obj.hide_select = False
            obj.select_set(True)
        context.view_layer.objects.active = source
        scene.frame_start, scene.frame_end = start, end
        if export_root_name and source.name != export_root_name:
            if name_collision and name_collision != source:
                renamed_objects.append((name_collision, name_collision.name))
                name_collision.name = "__UE4_EXPORT_DISPLACED_ROOT__"
            renamed_objects.append((source, source.name))
            source.name = export_root_name
        if character:
            # Write reference transforms, rather than the animator's current pose,
            # both to the FBX skeleton defaults and to the skinned mesh.
            set_enum(source.data, "pose_position", "REST")
            context.view_layer.update()
        if do_root_motion:
            # Route master motion into the exported root object. Bone WORLD constraints
            # compensate for its movement, preventing motion from being applied twice.
            raw_offset = list(rig.get("ue4_source_offset", [value for row in Matrix.Identity(4) for value in row]))
            offset = Matrix([raw_offset[i:i + 4] for i in range(0, 16, 4)])
            master_rest_inv = rig.data.bones["CTRL_master"].matrix_local.inverted()
            root_samples = []
            for frame in range(start, end + 1):
                scene.frame_set(frame)
                context.view_layer.update()
                root_samples.append((frame, rig.matrix_world @ rig.pose.bones["CTRL_master"].matrix @ master_rest_inv @ offset))
            source.animation_data_create()
            temporary_action = bpy.data.actions.new("__UE4_EXPORT_ROOT__")
            source.animation_data.action = temporary_action
            set_enum(source, "rotation_mode", "QUATERNION")
            for frame, matrix in root_samples:
                source.matrix_world = matrix
                for prop in ("location", "rotation_quaternion", "scale"):
                    source.keyframe_insert(data_path=prop, frame=frame, group="Root Motion")
            for layer in temporary_action.layers:
                for strip in layer.strips:
                    for bag in strip.channelbags:
                        for curve in bag.fcurves:
                            for key in curve.keyframe_points:
                                set_enum(key, "interpolation", "LINEAR")
        options = {
            "use_selection": True, "use_visible": False,
            "object_types": {"ARMATURE", "MESH"} if character else {"ARMATURE"},
            "global_scale": 1.0, "apply_unit_scale": True,
            "apply_scale_options": "FBX_SCALE_NONE",
            "axis_forward": "-Y", "axis_up": "Z",
            "primary_bone_axis": "Y", "secondary_bone_axis": "X",
            "use_space_transform": True, "bake_space_transform": False,
            "add_leaf_bones": False, "use_armature_deform_only": False,
            "armature_nodetype": "NULL",
            "use_mesh_modifiers": True, "mesh_smooth_type": "FACE",
            "bake_anim": not character,
            "bake_anim_use_all_bones": True,
            "bake_anim_use_nla_strips": False, "bake_anim_use_all_actions": False,
            "bake_anim_force_startend_keying": True,
            "bake_anim_step": 1.0, "bake_anim_simplify_factor": 0.0,
            "path_mode": "AUTO", "embed_textures": False, "use_custom_props": False,
        }
        props = bpy.ops.export_scene.fbx.get_rna_type().properties
        for name, value in options.items():
            prop = props[name]
            if prop.type == "ENUM":
                valid = {item.identifier for item in prop.enum_items}
                if not (value <= valid if isinstance(value, set) else value in valid):
                    raise RuntimeError(f"Unsupported FBX option: {name}={value}")
        fd, temporary_path = tempfile.mkstemp(prefix=".ue4_export_", suffix=".fbx", dir=os.path.dirname(filepath))
        os.close(fd)
        result = bpy.ops.export_scene.fbx(filepath=temporary_path, check_existing=False, **options)
        if "FINISHED" not in result or os.path.getsize(temporary_path) == 0:
            raise RuntimeError("FBX export did not complete")
        if os.path.isfile(filepath):
            shutil.copy2(filepath, filepath + ".bak")
        os.replace(temporary_path, filepath)
        temporary_path = None
        return {"filepath": filepath, "frames": (start, end), "bones": len(source.data.bones),
                "meshes": len(meshes), "root_motion": bool(do_root_motion), "held_pose": held_pose}
    finally:
        if temporary_action is not None:
            source.animation_data.action = snapshot["action"]
            if snapshot["action"] and snapshot["slot"]:
                source.animation_data.action_slot = snapshot["slot"]
            if not snapshot["had_animation_data"]:
                source.animation_data_clear()
            bpy.data.actions.remove(temporary_action)
        set_enum(source, "rotation_mode", snapshot["source_rotation_mode"])
        set_enum(source.data, "pose_position", snapshot["source_pose_position"])
        for obj, name in reversed(renamed_objects):
            obj.name = name
        scene.frame_start, scene.frame_end = snapshot["range"]
        scene.frame_set(snapshot["frame"], subframe=snapshot["subframe"])
        for obj, hidden, locked, selected, matrix in snapshot["objects"]:
            obj.matrix_basis = matrix
            obj.hide_set(hidden)
            obj.hide_select = locked
            obj.select_set(selected)
        for obj, matrices in snapshot["poses"].items():
            for name, matrix in matrices.items():
                obj.pose.bones[name].matrix_basis = matrix
        context.view_layer.objects.active = snapshot["active"]
        context.view_layer.update()
        if snapshot["active"] and snapshot["mode"] != "OBJECT":
            object_mode(snapshot["mode"])
        scene.tool_settings.use_keyframe_insert_auto = snapshot["autokey"]
        if temporary_path and os.path.isfile(temporary_path):
            os.remove(temporary_path)


_UE4_REQUIRED = (
    "pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "head",
    "clavicle_l", "upperarm_l", "lowerarm_l", "hand_l",
    "clavicle_r", "upperarm_r", "lowerarm_r", "hand_r",
    "thigh_l", "calf_l", "foot_l", "thigh_r", "calf_r", "foot_r",
)


def linked_control_rig(rig, source):
    if not isinstance(rig, bpy.types.Object) or rig.type != "ARMATURE" or rig == source:
        return False
    controls = ("CTRL_master", "CTRL_hand_IK_l", "CTRL_hand_IK_r", "CTRL_foot_IK_l", "CTRL_foot_IK_r")
    return all(name in rig.pose.bones for name in controls) and (
        rig.get("ue4_source_object") == source or rig.get("source_skeleton") == source.name)


def find_control_rig(scene, source):
    linked = source.get("ue4_control_rig")
    if linked_control_rig(linked, source) and linked.name in scene.objects:
        return linked
    return next((obj for obj in scene.objects if linked_control_rig(obj, source)), None)


def source_for_controls(context):
    active = context.view_layer.objects.active
    settings = context.scene.ue4_export_settings
    if active and active.type == "ARMATURE" and not active.get("source_skeleton"):
        return active
    if active and active.type == "MESH":
        sources = [mod.object for mod in active.modifiers if mod.type == "ARMATURE" and mod.object]
        if len(set(sources)) == 1:
            return sources[0]
    candidates = set()
    for obj in context.selected_objects:
        if obj.type == "ARMATURE" and not obj.get("source_skeleton"):
            candidates.add(obj)
        elif obj.type == "MESH":
            candidates.update(mod.object for mod in obj.modifiers if mod.type == "ARMATURE" and mod.object)
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise RuntimeError("Select one UE4 skeleton or one skinned character")
    if settings.source_rig:
        return settings.source_rig
    if active and active.type == "ARMATURE":
        return active.get("ue4_source_object") or context.scene.objects.get(active.get("source_skeleton", ""))
    raise RuntimeError("Select a UE4 skeleton or its skinned mesh, or choose UE4 skeleton above")


def validate_source(source, context):
    if source.type != "ARMATURE" or source.name not in context.view_layer.objects:
        raise RuntimeError("Choose a UE4 armature in the current view layer")
    if source.library and not source.override_library:
        raise RuntimeError("Make the linked armature local before creating controls")
    missing = [name for name in _UE4_REQUIRED if name not in source.data.bones]
    if missing:
        raise RuntimeError("Not a standard UE4 skeleton; missing bones: " + ", ".join(missing))
    if (source.data.bones["head"].head_local - source.data.bones["pelvis"].head_local).length < 1e-6 or (
        source.data.bones["upperarm_l"].head_local - source.data.bones["upperarm_r"].head_local).length < 1e-6:
        raise RuntimeError("The skeleton has degenerate body proportions")
    for side in ("l", "r"):
        for child, parent in (("lowerarm", "upperarm"), ("hand", "lowerarm"), ("calf", "thigh"), ("foot", "calf")):
            bone = source.data.bones[child + "_" + side]
            if bone.parent != source.data.bones[parent + "_" + side]:
                raise RuntimeError(f"{bone.name} must be parented to {parent}_{side}")
    if any(pose.constraints for pose in source.pose.bones) or source.constraints:
        raise RuntimeError("This skeleton already has constraints; use an unrigged UE4 skeleton")
    ad = source.animation_data
    if ad and (ad.action or any(not track.mute and track.strips for track in ad.nla_tracks)):
        raise RuntimeError("Import the UE4 character without animations before building controls")
    if any(max(abs(p.matrix_basis[i][j] - (1.0 if i == j else 0.0)) for i in range(4) for j in range(4)) > 1e-5
           for p in source.pose.bones):
        raise RuntimeError("Reset the source armature pose before building controls")
    scales = source.matrix_world.to_scale()
    if min(scales) <= 0 or max(scales) - min(scales) > max(scales) * 1e-4:
        raise RuntimeError("The skeleton needs a positive uniform object scale")
    for side in ("l", "r"):
        for a, b, c in (("upperarm", "lowerarm", "hand"), ("thigh", "calf", "foot")):
            points = [source.data.bones[name + "_" + side].head_local for name in (a, b, c)]
            if (points[1] - points[0]).length < 1e-6 or (points[2] - points[1]).length < 1e-6:
                raise RuntimeError(f"Zero-length limb near {b}_{side}")


def select_control_rig(context, rig):
    if context.object and context.object.mode != "OBJECT":
        object_mode("OBJECT")
    for obj in context.selected_objects:
        obj.select_set(False)
    rig.hide_set(False)
    rig.hide_select = False
    rig.select_set(True)
    context.view_layer.objects.active = rig
    object_mode("POSE")


def set_pose_selection(rig, name):
    # Blender 5.2 moved pose selection from Bone to PoseBone.
    use_pose = "select" in bpy.types.PoseBone.bl_rna.properties
    for pose in rig.pose.bones:
        (pose if use_pose else pose.bone).select = pose.name == name
    rig.data.bones.active = rig.data.bones[name]


def character_axes(source):
    bones = source.data.bones
    up = (bones["head"].head_local - bones["pelvis"].head_local).normalized()
    lateral = bones["upperarm_l"].head_local - bones["upperarm_r"].head_local
    lateral = (lateral - up * lateral.dot(up)).normalized()
    forward = up.cross(lateral).normalized()
    if all("ball_" + side in bones for side in ("l", "r")):
        feet_forward = sum((bones["ball_" + side].head_local - bones["foot_" + side].head_local for side in ("l", "r")), Vector())
        if forward.dot(feet_forward) < 0:
            forward.negate()
    else:
        # The standard UE4 character faces -Y in Blender coordinates.
        if forward.y > 0:
            forward.negate()
    basis = Matrix((lateral, -forward, up)).transposed()
    unit = (bones["head"].head_local - bones["pelvis"].head_local).length / 68.75
    return up, forward, basis, max(unit, 1e-6)


def pole_position(a, b, c, fallback, distance_factor=0.65):
    axis = c - a
    if axis.length < 1e-6:
        axis = b - a
    axis.normalize()
    bend = b - (a + axis * (b - a).dot(axis))
    length = (b - a).length + (c - b).length
    if bend.length < length * 1e-5:
        bend = fallback - axis * fallback.dot(axis)
        if bend.length < 1e-6:
            bend = axis.orthogonal()
    return b + bend.normalized() * length * distance_factor


def make_control_shapes(context, rig, unit, basis, owner):
    data = rig.data
    collections = {name: data.collections.new(name) for name in
                   ("Body", "Hands and Feet IK", "Elbows and Knees", "Fingers FK", "Mechanism - hidden")}
    collections["Fingers FK"].is_visible = True
    collections["Mechanism - hidden"].is_visible = False
    fingers = ("thumb", "index", "middle", "ring", "pinky")
    for bone in data.bones:
        if bone.name.startswith("MCH_"):
            group = "Mechanism - hidden"
        elif "_IK_" in bone.name:
            group = "Hands and Feet IK"
        elif bone.name.startswith(("CTRL_elbow", "CTRL_knee")):
            group = "Elbows and Knees"
        elif any(bone.name.startswith("CTRL_" + finger) for finger in fingers):
            group = "Fingers FK"
        else:
            group = "Body"
        collections[group].assign(bone)
    shapes_collection = bpy.data.collections.new(rig.name + " Shapes")
    owner.children.link(shapes_collection)

    def shape(name, vertices, edges):
        mesh = bpy.data.meshes.new("WGT_" + name)
        mesh.from_pydata([tuple(value * unit for value in vertex) for vertex in vertices], edges, [])
        mesh.update()
        obj = bpy.data.objects.new("WGT_" + name, mesh)
        shapes_collection.objects.link(obj)
        obj.hide_render = True
        obj.hide_set(True)
        return obj

    def box(name, low, high):
        vertices = [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
        edges = [(i, j) for i in range(8) for j in range(i + 1, 8) if (i ^ j) in (1, 2, 4)]
        return shape(name, vertices, edges)

    def ring(name, rx, ry, vertical=False):
        vertices = [(rx * math.cos(i * math.tau / 48), ry * math.sin(i * math.tau / 48), 0) for i in range(48)]
        if vertical:
            vertices = [(x, 0, y) for x, y, _ in vertices]
        return shape(name, vertices, [(i, (i + 1) % 48) for i in range(48)])

    shapes = {
        "master": ring("master", 42, 42),
        "pelvis": box("pelvis", (-18, -10, -4), (18, 10, 4)),
        "spine": ring("spine", 15, 4, True), "chest": ring("chest", 25, 6, True),
        "head": ring("head", 12, 10, True), "neck": ring("neck", 8, 3, True),
        "hand": box("hand", (-5, -5, -5), (5, 5, 5)),
        "foot": box("foot", (-8, -23, -12), (8, 9, -5)),
        "small": box("small", (-2, -2, -2), (2, 2, 2)),
        "clavicle": box("clavicle", (-5, -3, -3), (5, 3, 3)),
        "toe": ring("toe", 7, 3, True),
        "pole": shape("pole", [(5, 0, 0), (-5, 0, 0), (0, 5, 0), (0, -5, 0), (0, 0, 5), (0, 0, -5)],
                      [(i, j) for i in range(6) for j in range(i + 1, 6) if i // 2 != j // 2]),
    }
    for pose in rig.pose.bones:
        name = pose.name
        if name.startswith("MCH_"):
            continue
        kind = "small"
        if name == "CTRL_master": kind = "master"
        elif name == "CTRL_pelvis": kind = "pelvis"
        elif name == "CTRL_spine_03": kind = "chest"
        elif name.startswith("CTRL_spine"): kind = "spine"
        elif name == "CTRL_head": kind = "head"
        elif name == "CTRL_neck_01": kind = "neck"
        elif name.startswith("CTRL_hand_IK"): kind = "hand"
        elif name.startswith("CTRL_foot_IK"): kind = "foot"
        elif name.startswith(("CTRL_elbow", "CTRL_knee")): kind = "pole"
        elif name.startswith("CTRL_clavicle"): kind = "clavicle"
        elif name.startswith("CTRL_ball"): kind = "toe"
        pose.custom_shape = shapes[kind]
        pose.use_custom_shape_bone_size = False
        pose.custom_shape_rotation_euler = (pose.bone.matrix_local.to_3x3().inverted() @ basis).to_euler()
        if hasattr(pose, "custom_shape_wire_width"):
            pose.custom_shape_wire_width = 2
        set_enum(pose.color, "palette", "CUSTOM")
        rgb = (0.95, 0.22, 0.15) if name.endswith("_l") else (0.18, 0.48, 1.0) if name.endswith("_r") else (1.0, 0.72, 0.12)
        pose.color.custom.normal = rgb
        pose.color.custom.select = (1.0, 0.9, 0.35)
        pose.color.custom.active = (1.0, 1.0, 0.7)
        pose.lock_scale = (True,) * 3
        if not ("_IK_" in name or name in ("CTRL_master", "CTRL_pelvis") or name.startswith(("CTRL_elbow", "CTRL_knee"))):
            pose.lock_location = (True,) * 3
        if name.startswith(("CTRL_elbow", "CTRL_knee")):
            pose.lock_rotation = (True,) * 3


_FINGER_NAMES = ("thumb", "index", "middle", "ring", "pinky")


def finger_chains(rig):
    return {(side, finger): [f"CTRL_{finger}_{index:02d}_{side}" for index in range(1, 4)]
            for side in ("l", "r") for finger in _FINGER_NAMES
            if all(f"CTRL_{finger}_{index:02d}_{side}" in rig.data.bones for index in range(1, 4))}


def finger_frame(direction, inward):
    y = direction.normalized()
    inward = inward - y * inward.dot(y)
    if inward.length < 1e-6:
        inward = y.orthogonal()
    x = y.cross(inward).normalized()
    z = x.cross(y).normalized()
    return Matrix((x, y, z)).transposed()


def add_finger_controls(context, rig=None, source=None):
    """Add additive curl pivots without changing existing FK names, rest matrices or animation paths."""
    rig = rig or rigs(context)[0]
    if rig.type != "ARMATURE":
        raise RuntimeError("Choose a UE4 control rig")
    source = source or rig.get("ue4_source_object") or context.scene.objects.get(rig.get("source_skeleton", ""))
    if source is None:
        raise RuntimeError("The original UE4 skeleton could not be found")
    chains = finger_chains(rig)
    if not chains:
        return {"rig": rig, "fk": 0, "curl": 0, "grip": 0, "reused": True}
    if rig.get("ue4_finger_controls_version", 0) >= 1:
        for name in ("Fingers FK", "Fingers Curl"):
            if name in rig.data.collections:
                rig.data.collections[name].is_visible = True
        return {"rig": rig, "fk": len(chains) * 3, "curl": len(chains),
                "grip": len({side for side, _ in chains}), "reused": True}
    for side, finger in chains:
        if f"CTRL_curl_{finger}_{side}" in rig.data.bones or any(
                name.replace("CTRL_", "MCH_curl_", 1) in rig.data.bones for name in chains[side, finger]):
            raise RuntimeError("Finger control names are already in use; cannot safely upgrade this rig")
    sides = sorted({side for side, _ in chains})
    if any(f"CTRL_grip_{side}" in rig.data.bones for side in sides):
        raise RuntimeError("Grip control names are already in use")
    _, _, _, unit = character_axes(source)
    frames, centers, inwards, directions = {}, {}, {}, {}
    for side in sides:
        bases = [rig.data.bones[names[0]].head_local.copy() for (s, f), names in chains.items() if s == side and f != "thumb"]
        if not bases:
            bases = [rig.data.bones[names[0]].head_local.copy() for (s, f), names in chains.items() if s == side]
        center = sum(bases, Vector()) / len(bases)
        centers[side] = center
        longitudinal = sum((rig.data.bones[names[2]].head_local - rig.data.bones[names[0]].head_local
                            for (s, f), names in chains.items() if s == side and f != "thumb"), Vector())
        if longitudinal.length < 1e-6:
            names = next(names for (s, _), names in chains.items() if s == side)
            longitudinal = rig.data.bones[names[2]].head_local - rig.data.bones[names[0]].head_local
        longitudinal.normalize()
        if (side, "index") in chains and (side, "pinky") in chains:
            across = rig.data.bones[chains[side, "index"][0]].head_local - rig.data.bones[chains[side, "pinky"][0]].head_local
        else:
            across = rig.data.bones[f"MCH_follow_hand_{side}"].matrix_local.to_3x3().col[0]
        inward = across.cross(longitudinal)
        if inward.length < 1e-6:
            inward = longitudinal.orthogonal()
        inward.normalize()
        if (side, "thumb") in chains:
            thumb_tip = rig.data.bones[chains[side, "thumb"][2]].head_local
            if inward.dot(thumb_tip - center) < 0:
                inward.negate()
        elif side == "l":
            inward.negate()
        inwards[side], directions[side] = inward, longitudinal
        for (s, finger), names in chains.items():
            if s != side: continue
            direction = rig.data.bones[names[2]].head_local - rig.data.bones[names[0]].head_local
            goal = center - rig.data.bones[names[2]].head_local if finger == "thumb" else inward
            frames[side, finger] = finger_frame(direction, goal)
    old_mode = context.object.mode if context.object else "OBJECT"
    old_active, old_selected = context.view_layer.objects.active, list(context.selected_objects)
    old_hidden, old_locked = rig.hide_get(), rig.hide_select
    old_autokey = context.scene.tool_settings.use_keyframe_insert_auto
    original_pose = {p.name: p.matrix_basis.copy() for p in rig.pose.bones}
    old_parents = {name: rig.data.bones[name].parent.name if rig.data.bones[name].parent else None
                   for names in chains.values() for name in names}
    old_shapes = {name: (rig.pose.bones[name].custom_shape,
                        rig.pose.bones[name].custom_shape_rotation_euler.copy(),
                        rig.pose.bones[name].custom_shape_scale_xyz.copy(),
                        rig.pose.bones[name].use_custom_shape_bone_size)
                  for name in old_parents}
    collections_before = {col.name: col.is_visible for col in rig.data.collections}
    created, driver_paths = [], []
    shape_collection = None
    try:
        context.scene.tool_settings.use_keyframe_insert_auto = False
        if context.object and context.object.mode != "OBJECT": object_mode("OBJECT")
        rig.hide_set(False); rig.hide_select = False
        for obj in context.selected_objects: obj.select_set(False)
        rig.select_set(True); context.view_layer.objects.active = rig
        object_mode("EDIT")
        for side in sides:
            hand = rig.data.edit_bones[f"MCH_follow_hand_{side}"]
            center, inward, direction = centers[side], inwards[side], directions[side]
            grip = rig.data.edit_bones.new(f"CTRL_grip_{side}"); created.append(grip.name)
            matrix = finger_frame(direction, inward).to_4x4()
            wristward = hand.head - center
            if wristward.length > 1e-6: wristward.normalize()
            position = center - inward * 9 * unit + wristward * 4 * unit
            matrix.translation = position
            grip.head, grip.tail = position, position + matrix.to_3x3().col[1] * 3 * unit
            grip.matrix, grip.length, grip.parent, grip.use_deform = matrix, 3 * unit, hand, False
        for (side, finger), names in chains.items():
            hand = rig.data.edit_bones[f"MCH_follow_hand_{side}"]
            base = rig.data.edit_bones[names[0]].head.copy()
            frame = frames[side, finger]
            position = centers[side] + (base - centers[side]) * 1.6 - inwards[side] * 4 * unit
            if finger == "thumb":
                position = base - inwards[side] * 6 * unit + frame.col[1] * 4 * unit
            matrix = frame.to_4x4(); matrix.translation = position
            curl = rig.data.edit_bones.new(f"CTRL_curl_{finger}_{side}"); created.append(curl.name)
            curl.head, curl.tail = position, position + frame.col[1] * 2 * unit
            curl.matrix, curl.length, curl.parent, curl.use_deform = matrix, 2 * unit, hand, False
            for name in names:
                bone = rig.data.edit_bones[name]
                original_matrix, length, parent = bone.matrix.copy(), bone.length, bone.parent
                pivot = rig.data.edit_bones.new(name.replace("CTRL_", "MCH_curl_", 1)); created.append(pivot.name)
                pivot.head, pivot.tail = bone.head.copy(), bone.tail.copy()
                pivot.matrix, pivot.length, pivot.parent, pivot.use_deform = original_matrix, length, parent, False
                pivot.use_inherit_rotation = bone.use_inherit_rotation
                set_enum(pivot, "inherit_scale", bone.inherit_scale)
                bone.parent, bone.use_connect = pivot, False
                bone.matrix, bone.length = original_matrix, length
        object_mode("POSE")
        fk_group = rig.data.collections.get("Fingers FK") or rig.data.collections.new("Fingers FK")
        curl_group = rig.data.collections.get("Fingers Curl") or rig.data.collections.new("Fingers Curl")
        mechanism = rig.data.collections.get("Mechanism - hidden") or rig.data.collections.new("Mechanism - hidden")
        fk_group.is_visible = curl_group.is_visible = True
        mechanism.is_visible = False
        for name in created:
            (mechanism if name.startswith("MCH_") else curl_group).assign(rig.data.bones[name])
        for (side, finger), names in chains.items():
            curl_name, grip_name = f"CTRL_curl_{finger}_{side}", f"CTRL_grip_{side}"
            for index, name in enumerate(names):
                fk_group.assign(rig.data.bones[name])
                pivot = rig.pose.bones[name.replace("CTRL_", "MCH_curl_", 1)]
                set_enum(pivot, "rotation_mode", "QUATERNION")
                axis = (pivot.bone.matrix_local.to_3x3().inverted() @ frames[side, finger].col[0]).normalized()
                weight = ((0.35, 0.70, 0.70) if finger == "thumb" else (0.65, 1.0, 0.60))[index]
                grip_weight = 0.55 if finger == "thumb" else 1.0
                angle = f"((curl + {grip_weight}*grip)*{weight})"
                for component in range(4):
                    curve = pivot.driver_add("rotation_quaternion", component)
                    driver_paths.append((pivot.name, "rotation_quaternion", component))
                    driver = curve.driver
                    set_enum(driver, "type", "SCRIPTED")
                    for variable_name, control in (("curl", curl_name), ("grip", grip_name)):
                        variable = driver.variables.new(); variable.name = variable_name
                        set_enum(variable, "type", "SINGLE_PROP")
                        variable.targets[0].id = rig
                        variable.targets[0].data_path = f'pose.bones["{control}"].rotation_euler[0]'
                    driver.expression = f"cos({angle}/2)" if component == 0 else f"{axis[component-1]:.12g}*sin({angle}/2)"
        shape_collection = bpy.data.collections.new(rig.name + " Finger Shapes")
        (rig.users_collection[0] if rig.users_collection else context.scene.collection).children.link(shape_collection)
        def ring(name, arrow=False):
            mesh = bpy.data.meshes.new(name)
            vertices = [(0, math.cos(i*math.tau/32), math.sin(i*math.tau/32)) for i in range(32)]
            edges = [(i,(i+1)%32) for i in range(32)]
            if arrow:
                vertices.extend([(0,1.0,0.0),(0,0.72,0.27),(0,0.72,-0.27)])
                edges.extend([(32,33),(32,34)])
            mesh.from_pydata(vertices,edges,[]);mesh.update()
            obj = bpy.data.objects.new(name,mesh);shape_collection.objects.link(obj)
            obj.hide_render=True;obj.hide_set(True)
            return obj
        fk_shape, curl_shape = ring("WGT_finger_joint"), ring("WGT_finger_curl",True)
        for (side,finger),names in chains.items():
            for name in names:
                pose=rig.pose.bones[name]
                pose.custom_shape=fk_shape;pose.use_custom_shape_bone_size=False
                pose.custom_shape_scale_xyz=(0.85*unit,)*3
                pose.custom_shape_rotation_euler=(pose.bone.matrix_local.to_3x3().inverted()@frames[side,finger]).to_euler()
                if hasattr(pose,"custom_shape_wire_width"):pose.custom_shape_wire_width=2
        for name in created:
            if name.startswith("MCH_"):continue
            pose=rig.pose.bones[name]
            set_enum(pose,"rotation_mode","XYZ")
            pose.lock_location=pose.lock_scale=(True,)*3
            pose.lock_rotation=(False,True,True)
            pose.custom_shape=curl_shape;pose.use_custom_shape_bone_size=False
            radius=(2.5 if name.startswith("CTRL_grip_") else 1.4)*unit
            pose.custom_shape_scale_xyz=(radius,)*3
            pose.custom_shape_rotation_euler=(0,0,0)
            if hasattr(pose,"custom_shape_wire_width"):pose.custom_shape_wire_width=3
            set_enum(pose.color,"palette","CUSTOM")
            pose.color.custom.normal=(1.0,0.65,0.1) if name.startswith("CTRL_grip_") else ((1.0,0.35,0.2) if name.endswith("_l") else (0.25,0.65,1.0))
            pose.color.custom.select=(1.0,0.9,0.35);pose.color.custom.active=(1.0,1.0,0.7)
        for name,matrix in original_pose.items():rig.pose.bones[name].matrix_basis=matrix
        rig.update_tag();context.view_layer.update()
        rig["ue4_finger_controls_version"]=1
        return {"rig":rig,"fk":len(chains)*3,"curl":len(chains),"grip":len(sides),"reused":False}
    except Exception:
        if context.object and context.object.mode!="OBJECT":object_mode("OBJECT")
        for name,path,index in driver_paths:
            if name in rig.pose.bones:rig.pose.bones[name].driver_remove(path,index)
        object_mode("EDIT")
        for name,parent in old_parents.items():
            rig.data.edit_bones[name].parent=rig.data.edit_bones.get(parent) if parent else None
        for name in reversed(created):
            if name in rig.data.edit_bones:rig.data.edit_bones.remove(rig.data.edit_bones[name])
        object_mode("OBJECT")
        for name,values in old_shapes.items():
            pose=rig.pose.bones[name]
            pose.custom_shape,pose.custom_shape_rotation_euler,pose.custom_shape_scale_xyz,pose.use_custom_shape_bone_size=values
        for col in list(rig.data.collections):
            if col.name not in collections_before:rig.data.collections.remove(col)
            else:col.is_visible=collections_before[col.name]
        if shape_collection:
            for obj in list(shape_collection.objects):
                mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
                if mesh.users==0:bpy.data.meshes.remove(mesh)
            bpy.data.collections.remove(shape_collection)
        for name,matrix in original_pose.items():rig.pose.bones[name].matrix_basis=matrix
        raise
    finally:
        if context.object and context.object.mode!="OBJECT":object_mode("OBJECT")
        for obj in context.selected_objects:obj.select_set(False)
        rig.hide_set(old_hidden);rig.hide_select=old_locked
        for obj in old_selected:obj.select_set(True)
        context.view_layer.objects.active=old_active
        if old_active and old_mode!="OBJECT":object_mode(old_mode)
        context.scene.tool_settings.use_keyframe_insert_auto=old_autokey


def create_ue4_controls(context, source=None):
    source = source or source_for_controls(context)
    if source is None or source.type != "ARMATURE":
        raise RuntimeError("Choose an original UE4 skeleton")
    existing = find_control_rig(context.scene, source)
    if existing:
        settings = context.scene.ue4_export_settings
        settings.control_rig, settings.source_rig = existing, source
        select_control_rig(context, existing)
        add_finger_controls(context,existing,source)
        return {"rig": existing, "source": source, "reused": True}
    validate_source(source, context)
    up_axis, forward, basis, unit = character_axes(source)
    previous = {
        "active": context.view_layer.objects.active, "mode": context.object.mode if context.object else "OBJECT",
        "selection": list(context.selected_objects), "hidden": source.hide_get(), "locked": source.hide_select,
        "in_front": source.show_in_front, "autokey": context.scene.tool_settings.use_keyframe_insert_auto,
        "pose_position": source.data.pose_position, "source_link": source.get("ue4_control_rig"),
    }
    settings = context.scene.ue4_export_settings
    previous_settings = (settings.control_rig, settings.source_rig, settings.action, settings.slot_identifier,
                         settings.batch_index, [(item.action, item.enabled, item.slot_identifier) for item in settings.batch_actions])
    owner = rig = None
    added_constraints = []
    try:
        context.scene.tool_settings.use_keyframe_insert_auto = False
        if context.object and context.object.mode != "OBJECT": object_mode("OBJECT")
        owner = bpy.data.collections.new(source.name + " Controls")
        context.scene.collection.children.link(owner)
        data = bpy.data.armatures.new("UE4_ControlRig")
        rig = bpy.data.objects.new("UE4_ControlRig", data)
        owner.objects.link(rig)
        rig.matrix_world = source.matrix_world.copy()
        rig.show_in_front = True
        set_enum(data, "display_type", "WIRE")
        for obj in context.selected_objects: obj.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        mechanical = {a + "_" + side for side in ("l", "r") for a in ("upperarm", "lowerarm", "hand", "thigh", "calf", "foot")}
        mapping = {bone.name: ("MCH_follow_" if bone.name in mechanical or bone.name == "root" or "twist" in bone.name or bone.name.startswith("ik_") else "CTRL_") + bone.name for bone in source.data.bones}
        object_mode("EDIT")
        master = data.edit_bones.new("CTRL_master")
        master.head = source.data.bones["root"].head_local if "root" in source.data.bones else Vector()
        master.tail = master.head + up_axis * 15 * unit
        master.use_deform = False
        for bone in source.data.bones:
            edit = data.edit_bones.new(mapping[bone.name])
            # Matrix assignment needs a nonzero head/tail vector first.
            edit.head, edit.tail = bone.head_local.copy(), bone.tail_local.copy()
            edit.matrix, edit.length = bone.matrix_local.copy(), bone.length
            edit.use_deform = False
        for bone in source.data.bones:
            edit = data.edit_bones[mapping[bone.name]]
            edit.parent = data.edit_bones[mapping[bone.parent.name]] if bone.parent else master
            edit.use_connect = False
        limbs = []
        for side in ("l", "r"):
            for kind, a, b, end, parent in (("hand", "upperarm", "lowerarm", "hand", "clavicle_" + side),
                                          ("foot", "thigh", "calf", "foot", "pelvis")):
                an, bn, en = (name + "_" + side for name in (a, b, end))
                A, B, C = (source.data.bones[name].head_local.copy() for name in (an, bn, en))
                pole = pole_position(A, B, C, -forward if kind == "hand" else forward)
                axis = C - A
                if axis.length > 1e-6:
                    axis.normalize()
                    bend = B - (A + axis * (B - A).dot(axis))
                    length = (B - A).length + (C - B).length
                    if bend.length < length * 1e-5:
                        # A perfectly straight IK chain has no defined bend plane.
                        # Bias only the helper pivot; original bones keep their rest matrices.
                        B += (pole - B).normalized() * length * 0.001
                up_name, low_name = "MCH_IK_" + an, "MCH_IK_" + bn
                upper = data.edit_bones.new(up_name)
                upper.head, upper.tail = A, B
                upper.align_roll(source.data.bones[an].matrix_local.to_3x3().col[2])
                upper.parent = data.edit_bones[mapping[parent]]
                upper.use_deform = False
                lower = data.edit_bones.new(low_name)
                lower.head, lower.tail = B, C
                lower.align_roll(source.data.bones[bn].matrix_local.to_3x3().col[2])
                lower.parent, lower.use_connect, lower.use_deform = upper, True, False
                data.edit_bones[mapping[an]].parent = upper
                data.edit_bones[mapping[bn]].parent = lower
                data.edit_bones[mapping[en]].parent = lower
                target_name = "CTRL_" + kind + "_IK_" + side
                target = data.edit_bones.new(target_name)
                bone = source.data.bones[en]
                target.head, target.tail = bone.head_local.copy(), bone.tail_local.copy()
                target.matrix, target.length = bone.matrix_local.copy(), 10 * unit
                target.parent, target.use_deform = master, False
                pole_name = "CTRL_" + ("elbow" if kind == "hand" else "knee") + "_" + side
                pole_bone = data.edit_bones.new(pole_name)
                pole_bone.head, pole_bone.tail = pole, pole + up_axis * 7 * unit
                pole_bone.parent, pole_bone.use_deform = master, False
                limbs.append(dict(a=an, b=bn, end=en, up=up_name, low=low_name, target=target_name, pole=pole_name))
        object_mode("POSE")
        for pose in rig.pose.bones:
            set_enum(pose, "rotation_mode", "QUATERNION")
            pose.ik_stretch = 0

        def update():
            rig.update_tag()
            context.view_layer.update()

        def follow(pose, target, rotation=False):
            constraint = pose.constraints.new("COPY_ROTATION" if rotation else "COPY_TRANSFORMS")
            constraint.target, constraint.subtarget = rig, target
            set_enum(constraint, "owner_space", "WORLD")
            set_enum(constraint, "target_space", "WORLD")
            return constraint

        for limb in limbs:
            ik = rig.pose.bones[limb["low"]].constraints.new("IK")
            ik.name = "UE4 two-bone IK"
            ik.target, ik.subtarget, ik.pole_target, ik.pole_subtarget = rig, limb["target"], rig, limb["pole"]
            ik.chain_count, ik.use_stretch, ik.iterations = 2, False, 256
            follow(rig.pose.bones[mapping[limb["end"]]], limb["target"], rotation=True)
        update()
        for limb in limbs:
            ik = rig.pose.bones[limb["low"]].constraints[0]

            def loss(angle):
                ik.pole_angle = angle
                update()
                return sum((rig.pose.bones[name].matrix[i][j] - data.bones[name].matrix_local[i][j]) ** 2
                           for name in (limb["up"], limb["low"]) for i in range(3) for j in range(3))

            best = min((-math.pi + math.tau * i / 48 for i in range(48)), key=loss)
            left, right = best - math.tau / 48, best + math.tau / 48
            for _ in range(30):
                x, y = left + (right - left) / 3, right - (right - left) / 3
                if loss(x) < loss(y): right = y
                else: left = x
            ik.pole_angle = ((left + right) / 2 + math.pi) % math.tau - math.pi
            update()
        for side in ("l", "r"):
            for kind in ("hand", "foot"):
                name = "ik_" + kind + "_" + side
                if name in mapping: follow(rig.pose.bones[mapping[name]], mapping[kind + "_" + side])
        if "ik_hand_gun" in mapping:
            follow(rig.pose.bones[mapping["ik_hand_gun"]], mapping["hand_r"])
        update()
        # Reference-offset bones keep the exact original rest matrices, including
        # arbitrary FBX axes and the ambiguous roll of completely straight limbs.
        references = {name: rig.pose.bones[target].matrix.copy() for name, target in mapping.items()}
        parent_rests = {name: data.bones[target].matrix_local.copy() for name, target in mapping.items()}
        export_mapping = {}
        object_mode("EDIT")
        for name, target in mapping.items():
            rest = source.data.bones[name].matrix_local
            matrix = parent_rests[name] @ references[name].inverted() @ rest
            edit = data.edit_bones.new("MCH_export_" + name)
            edit.head = matrix.translation
            edit.tail = matrix.translation + matrix.to_3x3().col[1] * source.data.bones[name].length
            edit.matrix, edit.length = matrix, source.data.bones[name].length
            edit.parent, edit.use_deform = data.edit_bones[target], False
            export_mapping[name] = edit.name
        object_mode("POSE")
        update()
        for name, target in export_mapping.items():
            pose, rest = rig.pose.bones[target].matrix, source.data.bones[name].matrix_local
            angle = pose.to_quaternion().rotation_difference(rest.to_quaternion()).angle
            if (pose.translation - rest.translation).length > unit * 0.02 or min(angle, math.tau - angle) > 0.002:
                raise RuntimeError("Could not align export bones near " + name)
        make_control_shapes(context, rig, unit, basis, owner)
        add_finger_controls(context,rig,source)
        for pose in source.pose.bones:
            constraint = follow(pose, export_mapping[pose.name])
            added_constraints.append((pose, constraint))
            constraint.name = "UE4 ControlRig"
        rig["source_skeleton"] = source.name
        rig["ue4_source_object"] = source
        rig["ue4_generator_version"] = 1
        rig["ue4_source_offset"] = [value for row in Matrix.Identity(4) for value in row]
        if "root" not in source.data.bones:
            # Blender may consume the UE4 root joint as the armature object and
            # rename repeated imports to root.001. Keep the FBX root joint name.
            rig["ue4_export_root_name"] = "root"
        rig["instructions"] = "Pose Mode: G move IK/poles/pelvis, R rotate body/hands/feet. Finger joint rings: FK. Arrow rings: R X X to curl; large rings: grip. Export using UE Rig Helper."
        source["ue4_control_rig"] = rig
        set_enum(source.data, "pose_position", "POSE")
        source.hide_set(True)
        source.hide_select = True
        source.show_in_front = False
        settings.control_rig, settings.source_rig, settings.action = rig, source, None
        settings.batch_actions.clear()
        refresh_actions(context)
        set_pose_selection(rig, "CTRL_pelvis")
        update()
        return {"rig": rig, "source": source, "reused": False, "limbs": limbs, "mapping": mapping,
                "export_mapping": export_mapping}
    except Exception:
        if context.object and context.object.mode != "OBJECT": object_mode("OBJECT")
        for pose, constraint in added_constraints:
            pose.constraints.remove(constraint)
        if source.get("ue4_control_rig") == rig:
            if previous["source_link"] is None:
                del source["ue4_control_rig"]
            else:
                source["ue4_control_rig"] = previous["source_link"]
        settings.control_rig, settings.source_rig, settings.action = previous_settings[:3]
        settings.slot_identifier = previous_settings[3]
        settings.batch_index = previous_settings[4]
        settings.batch_actions.clear()
        for action, enabled, slot in previous_settings[5]:
            item = settings.batch_actions.add()
            item.action, item.enabled, item.slot_identifier = action, enabled, slot
        if owner:
            children = list(owner.children)
            for obj in list(owner.all_objects):
                block, kind = obj.data, obj.type
                bpy.data.objects.remove(obj, do_unlink=True)
                if block and block.users == 0:
                    if kind == "MESH": bpy.data.meshes.remove(block)
                    elif kind == "ARMATURE": bpy.data.armatures.remove(block)
            for child in children: bpy.data.collections.remove(child)
            bpy.data.collections.remove(owner)
        source.hide_set(previous["hidden"])
        source.hide_select = previous["locked"]
        source.show_in_front = previous["in_front"]
        set_enum(source.data, "pose_position", previous["pose_position"])
        for obj in context.selected_objects: obj.select_set(False)
        for obj in previous["selection"]: obj.select_set(True)
        context.view_layer.objects.active = previous["active"]
        if previous["active"] and previous["mode"] != "OBJECT": object_mode(previous["mode"])
        raise
    finally:
        context.scene.tool_settings.use_keyframe_insert_auto = previous["autokey"]


class UE4BatchAction(bpy.types.PropertyGroup):
    action: PointerProperty(type=bpy.types.Action)
    enabled: BoolProperty(name="Export", default=True)
    slot_identifier: EnumProperty(name="Action slot", items=batch_slot_items)


class UE4ExportSettings(bpy.types.PropertyGroup):
    control_rig: PointerProperty(name="Control rig", type=bpy.types.Object,
                                poll=lambda self, obj: obj.type == "ARMATURE")
    source_rig: PointerProperty(name="UE4 skeleton", type=bpy.types.Object,
                               poll=lambda self, obj: obj.type == "ARMATURE")
    directory: StringProperty(name="Export folder", subtype="DIR_PATH", default="//FBX/")
    action: PointerProperty(name="Action", type=bpy.types.Action, poll=action_poll,
                            description="Choose a clip to export; clearing this uses the rig's current Action")
    slot_identifier: EnumProperty(name="Action slot", items=selected_slot_items)
    batch_actions: CollectionProperty(type=UE4BatchAction)
    batch_index: IntProperty(default=0, min=0)
    show_finger_sliders: BoolProperty(name="Individual finger curls", default=False)
    frame_range: EnumProperty(name="Frames", items=[
        ("TIMELINE", "Timeline", "Use the scene Start and End frames"),
        ("ACTION", "Action", "Use the chosen Action's frame range")], default="ACTION")
    root_motion: BoolProperty(name="Root motion from Master", default=True,
                             description="Write CTRL_master motion into the exported root")


class UE4_OT_add_fingers(bpy.types.Operator):
    bl_idname = "ue4.add_finger_controls"
    bl_label = "Add / Update Finger Controls"
    bl_description = "Add visible joint FK rings, finger curls and grip controls to an existing UE4 control rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            result = add_finger_controls(context)
            if not result["fk"]:
                raise RuntimeError("This rig has no complete UE4 finger chains")
            self.report({"INFO"}, f"Fingers ready: {result['fk']} joint controls, {result['curl']} curls, {result['grip']} grips")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UE4_OT_create_controls(bpy.types.Operator):
    bl_idname = "ue4.create_controls"
    bl_label = "Create UE4 Controls"
    bl_description = "Select a UE4 armature or its skinned mesh. Create controls, or select its existing rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            result = create_ue4_controls(context)
            name = result["rig"].name
            self.report({"INFO"}, ("Controls already exist: " if result["reused"] else "Created controls: ") + name)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UE4_OT_export_fbx(bpy.types.Operator, ExportHelper):
    bl_idname = "ue4.export_fbx"
    bl_label = "Export UE4 FBX"
    bl_description = "Export the original UE4 skeleton; animation is baked from controllers automatically"
    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={"HIDDEN"})
    character: BoolProperty(default=False, options={"HIDDEN"})

    def invoke(self, context, event):
        try:
            rig, source = rigs(context)
            action = selected_action(context.scene.ue4_export_settings, rig)
            name = "UE4_Character" if self.character else filename_safe(action.name if action else "Animation")
            self.filepath = os.path.join(bpy.path.abspath(context.scene.ue4_export_settings.directory), name + ".fbx")
            return ExportHelper.invoke(self, context, event)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def execute(self, context):
        try:
            result = export_fbx(context, self.filepath, character=self.character)
            message = "Exported " + os.path.basename(result["filepath"])
            if result["held_pose"]:
                message += " (single pose held for 2 frames)"
            self.report({"INFO"}, message)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UE4_OT_refresh_actions(bpy.types.Operator):
    bl_idname = "ue4.refresh_actions"
    bl_label = "Refresh"
    bl_description = "Refresh the list of Actions compatible with the control rig"

    def execute(self, context):
        try:
            refresh_actions(context)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UE4_OT_select_actions(bpy.types.Operator):
    bl_idname = "ue4.select_actions"
    bl_label = "Select Actions"
    select: BoolProperty(default=True)

    def execute(self, context):
        for entry in context.scene.ue4_export_settings.batch_actions:
            entry.enabled = self.select
        return {"FINISHED"}


class UE4_OT_export_batch(bpy.types.Operator):
    bl_idname = "ue4.export_batch"
    bl_label = "Export Checked Actions"
    bl_description = "Export checked Actions into separate FBX files in Export folder"

    def execute(self, context):
        try:
            result = export_batch(context)
            count, failures = len(result["files"]), len(result["errors"])
            if failures:
                details = "; ".join(f"{error['action']}: {error['error']}" for error in result["errors"][:3])
                self.report({"WARNING"} if count else {"ERROR"}, f"Exported {count}; failed {failures}. {details}")
            else:
                self.report({"INFO"}, f"Exported {count} Actions to {result['directory']}")
            return {"FINISHED"} if count else {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UE4_UL_actions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        if item.action is None:
            row.label(text="Deleted Action - refresh")
            return
        row.label(text=item.action.name)
        if len(compatible_slots(item.action, configured_rig(data, context))) > 1:
            row.prop(item, "slot_identifier", text="")
        else:
            start, end = item.action.frame_range
            row.label(text=f"{math.floor(start)} - {max(math.ceil(end), math.floor(start) + 1)}")


class UE4_PT_export(bpy.types.Panel):
    bl_label = "UE Rig Helper"
    bl_idname = "UE4_PT_control_rig_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UE Rig Helper"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.ue4_export_settings
        setup = layout.box()
        setup.label(text="Create Controls")
        setup.prop(settings, "source_rig")
        setup.operator("ue4.create_controls", text="Create UE4 Controls")
        setup.label(text="Uses selected UE4 skeleton or skinned mesh")
        layout.prop(settings, "control_rig")
        layout.prop(settings, "directory")
        try:
            rig, source = rigs(context)
            fingers = layout.box()
            fingers.label(text="Finger Controls")
            fingers.operator("ue4.add_finger_controls", text="Add / Update Finger Controls")
            for group_name, label in (("Fingers FK", "Joint FK rings"), ("Fingers Curl", "Curl / grip rings")):
                group = rig.data.collections.get(group_name)
                if group: fingers.prop(group,"is_visible",text=label)
            if rig.get("ue4_finger_controls_version",0) >= 1:
                for side,label in (("l","Left grip"),("r","Right grip")):
                    pose=rig.pose.bones.get(f"CTRL_grip_{side}")
                    if pose:fingers.prop(pose,"rotation_euler",index=0,text=label)
                fingers.prop(settings,"show_finger_sliders")
                if settings.show_finger_sliders:
                    for side,hand in (("l","Left"),("r","Right")):
                        fingers.label(text=hand+" hand")
                        for finger in _FINGER_NAMES:
                            pose=rig.pose.bones.get(f"CTRL_curl_{finger}_{side}")
                            if pose:fingers.prop(pose,"rotation_euler",index=0,text=finger.title())
                fingers.label(text="Arrow rings: R X X | joint rings: FK")
            action = selected_action(settings, rig)
            box = layout.box()
            box.label(text="Single Action")
            box.prop(settings, "action")
            if settings.action is None:
                box.label(text="Current: " + (action.name if action else "none"))
            if action and len(compatible_slots(action, rig)) > 1:
                box.prop(settings, "slot_identifier")
            start, end = clip_range(context.scene, rig, action)
            box.prop(settings, "frame_range")
            box.label(text=f"Frames {start} - {end} | {context.scene.render.fps / context.scene.render.fps_base:g} fps")
            if start == end:
                box.label(text=f"Single pose: animation exports {start} - {start + 1}")
            layout.prop(settings, "root_motion")
            single_button = box.column()
            single_button.enabled = action is not None and bool(compatible_slots(action, rig))
            single_button.operator("ue4.export_fbx", text="Export Animation FBX").character = False
            batch = layout.box()
            batch.label(text="Batch Export")
            row = batch.row(align=True)
            row.operator("ue4.refresh_actions", text="Refresh")
            row.operator("ue4.select_actions", text="All").select = True
            row.operator("ue4.select_actions", text="None").select = False
            batch.template_list("UE4_UL_actions", "", settings, "batch_actions", settings, "batch_index", rows=4)
            count = sum(entry.enabled and entry.action is not None for entry in settings.batch_actions)
            batch.label(text="Each Action uses its own frame range")
            buttons = batch.column()
            buttons.enabled = count > 0
            buttons.operator("ue4.export_batch", text=f"Export Checked Actions ({count})")
            layout.separator()
            layout.operator("ue4.export_fbx", text="Export Character FBX").character = True
            layout.label(text="Original skeleton; controllers stay in Blender")
        except Exception as exc:
            layout.label(text=str(exc))


classes = (UE4BatchAction, UE4ExportSettings, UE4_OT_add_fingers, UE4_OT_create_controls, UE4_OT_export_fbx, UE4_OT_refresh_actions,
           UE4_OT_select_actions, UE4_OT_export_batch, UE4_UL_actions, UE4_PT_export)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ue4_export_settings = PointerProperty(type=UE4ExportSettings)


def unregister():
    del bpy.types.Scene.ue4_export_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

