"""FBX scene access: load, walk, and read one mesh's geometry + UVs.

The Autodesk SDK is imported here and nowhere else, so a machine without the
`fbx` wheel can still run everything that reads an already-extracted mesh JSON.

`pick_uv_element` and the mesh walk each existed in three copies before this
(extract_fbx, bake_uv, stitch.extract) — byte-identical in two cases and subtly
different in the third, which is how the multi-material bug below survived.
"""
import sys
from collections import Counter

import fbx

from python.common.paths import display


class FbxError(RuntimeError):
    """A scene could not be loaded or has nothing usable in it."""


def open_scene():
    """A fresh (manager, scene) pair with IO settings attached."""
    manager = fbx.FbxManager.Create()
    if not manager:
        raise FbxError("cannot create an FBX SDK manager")
    manager.SetIOSettings(fbx.FbxIOSettings.Create(manager, fbx.IOSROOT))
    return manager, fbx.FbxScene.Create(manager, "")


def load_scene(manager, scene, path):
    """Import `path` into `scene`. False when the importer refuses it."""
    importer = fbx.FbxImporter.Create(manager, "")
    if not importer.Initialize(str(path), -1, manager.GetIOSettings()):
        return False
    result = importer.Import(scene)
    importer.Destroy()
    return result


def save_scene(manager, scene, path):
    """Export `scene` to `path` in the native binary format."""
    exporter = fbx.FbxExporter.Create(manager, "")
    settings = manager.GetIOSettings()
    for prop in (fbx.EXP_FBX_MATERIAL, fbx.EXP_FBX_TEXTURE, fbx.EXP_FBX_SHAPE,
                 fbx.EXP_FBX_GOBO, fbx.EXP_FBX_ANIMATION,
                 fbx.EXP_FBX_GLOBAL_SETTINGS):
        settings.SetBoolProp(prop, True)
    settings.SetBoolProp(fbx.EXP_FBX_EMBEDDED, False)
    fmt = manager.GetIOPluginRegistry().GetNativeWriterFormat()
    result = exporter.Initialize(str(path), fmt, settings)
    if result:
        result = exporter.Export(scene)
    exporter.Destroy()
    return result


def mesh_nodes(root):
    """Every node carrying a mesh attribute, depth-first from `root`."""
    found = []

    def walk(node):
        for index in range(node.GetChildCount()):
            child = node.GetChild(index)
            attribute = child.GetNodeAttribute()
            if (attribute and attribute.GetAttributeType()
                    == fbx.FbxNodeAttribute.EType.eMesh):
                found.append(child)
            walk(child)

    walk(root)
    return found


def read_scene(path):
    """(manager, scene, mesh nodes) for `path`; the caller destroys the manager.

    The manager owns every node, so it must outlive the traversal — returning it
    is what lets the caller decide when the scene dies."""
    manager, scene = open_scene()
    if not load_scene(manager, scene, path):
        manager.Destroy()
        raise FbxError(f"cannot load {path}")
    nodes = mesh_nodes(scene.GetRootNode())
    if not nodes:
        manager.Destroy()
        raise FbxError(f"no mesh in {path}")
    return manager, scene, nodes


def node_matrix(node):
    """World matrix including the geometric transform.

    EvaluateGlobalTransform leaves the geometric transform out — it applies to
    the mesh only and is not inherited by children — but Maya bakes it into what
    the artist sees. Skipping it puts whole planes in the wrong place."""
    pivot = fbx.FbxNode.EPivotSet.eSourcePivot
    geometric = fbx.FbxAMatrix()
    geometric.SetTRS(node.GetGeometricTranslation(pivot),
                     node.GetGeometricRotation(pivot),
                     node.GetGeometricScaling(pivot))
    return node.EvaluateGlobalTransform() * geometric


def material_index(node):
    """The material actually painted on this mesh's polygons.

    Reading material 0 unconditionally is wrong for the newer modelling
    strategy, which attaches many materials and picks one per polygon
    (eByPolygon) — it made a 16-plane scene report one camera's texture for
    every plane. For eAllSame, or no material element, 0 is the answer."""
    mesh = node.GetMesh()
    element = mesh.GetElementMaterial() if mesh else None
    if element is None:
        return 0
    if element.GetMappingMode() != fbx.FbxLayerElement.EMappingMode.eByPolygon:
        return 0
    indices = element.GetIndexArray()
    if indices.GetCount() == 0:
        return 0
    counts = Counter(indices.GetAt(k) for k in range(indices.GetCount()))
    return counts.most_common(1)[0][0]


def diffuse_texture(node, index=None):
    """(uvset name, texture basename) of a material's diffuse FileTexture.

    `index` defaults to the material the polygons actually use."""
    if index is None:
        index = material_index(node)
    if index >= node.GetMaterialCount():
        return None, None
    prop = node.GetMaterial(index).FindProperty(fbx.FbxSurfaceMaterial.sDiffuse)
    if not prop.IsValid():
        return None, None
    criteria = fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId)
    if prop.GetSrcObjectCount(criteria) == 0:
        return None, None
    texture = prop.GetSrcObject(criteria, 0)
    name = str(texture.GetFileName()).replace("\\", "/").rsplit("/", 1)[-1]
    return str(texture.UVSet.Get()), name


def uv_element(mesh, uvset_name):
    """The layer element named `uvset_name`, else the first one."""
    count = mesh.GetElementUVCount()
    if count == 0:
        return None
    if uvset_name:
        for index in range(count):
            if mesh.GetElementUV(index).GetName() == uvset_name:
                return mesh.GetElementUV(index)
    return mesh.GetElementUV(0)


def uv_at(element, polygon_vertex, control_point):
    """One UV, resolving the element's mapping and reference modes."""
    direct = element.GetDirectArray()
    if element.GetMappingMode() == fbx.FbxLayerElement.EMappingMode.eByControlPoint:
        index = control_point
    else:                                    # eByPolygonVertex
        index = polygon_vertex
    if element.GetReferenceMode() == fbx.FbxLayerElement.EReferenceMode.eIndexToDirect:
        index = element.GetIndexArray().GetAt(index)
    value = direct.GetAt(index)
    return [value[0], value[1]]


def _constant_axis(points):
    """Index of the axis with ~zero span, plus the two that vary.

    Every plane in these scenes is flat in one world axis; dropping it turns a
    3-D mesh into the 2-D layout the renderer works in."""
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for point in points:
        for axis in range(3):
            low[axis] = min(low[axis], point[axis])
            high[axis] = max(high[axis], point[axis])
    spans = [high[axis] - low[axis] for axis in range(3)]
    constant = min(range(3), key=lambda axis: spans[axis])
    return constant, [axis for axis in range(3) if axis != constant], spans


def extract_mesh(node, tex_dir):
    """One mesh as the renderer's dict: 2-D triangles with UVs, plus texture."""
    mesh = node.GetMesh()
    matrix = node_matrix(node)
    world = []
    for index in range(mesh.GetControlPointsCount()):
        point = matrix.MultT(mesh.GetControlPointAt(index))
        world.append((point[0], point[1], point[2]))

    constant, kept, spans = _constant_axis(world)
    uvset, basename = diffuse_texture(node)
    element = uv_element(mesh, uvset)

    triangles = []
    polygon_vertex = 0
    for polygon in range(mesh.GetPolygonCount()):
        size = mesh.GetPolygonSize(polygon)
        vertices = []
        for corner in range(size):
            control_point = mesh.GetPolygonVertex(polygon, corner)
            position = world[control_point]
            vertices.append({
                "pos": [position[kept[0]], position[kept[1]]],
                "uv": uv_at(element, polygon_vertex, control_point) if element
                      else [0.0, 0.0],
            })
            polygon_vertex += 1
        # Fan-triangulate. These scenes are already triangulated, but a quad
        # would otherwise be silently dropped down to its first three corners.
        for corner in range(1, size - 1):
            triangles.append([vertices[0], vertices[corner], vertices[corner + 1]])

    return {
        "node": node.GetName(),
        "texture": display(tex_dir / basename) if basename else None,
        "texture_basename": basename,
        "uvset": uvset,
        "const_axis": constant,
        "kept_axes": kept,           # world axes that pos[0], pos[1] came from
        "spans": spans,
        "triangles": triangles,
    }
