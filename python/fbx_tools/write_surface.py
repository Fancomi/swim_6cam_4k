"""Write a small ASCII FBX surface without requiring the Autodesk SDK."""
from pathlib import Path


def write_surface(path, vertices, uv, triangles):
    """XY is the water plane in metres; UV uses the FBX bottom-left origin."""
    def values(rows):
        return ','.join(format(v, '.12g') for row in rows for v in row)

    indices = [(a, b, -c - 1) for a, b, c in triangles]
    Path(path).write_text('''; FBX 7.4.0 project file
FBXHeaderExtension: {
    FBXHeaderVersion: 1003
    FBXVersion: 7400
    Creator: "water_entry visual trial"
}
GlobalSettings: {
    Version: 1000
    Properties70: {
        P: "UpAxis", "int", "Integer", "",2
        P: "UpAxisSign", "int", "Integer", "",1
        P: "FrontAxis", "int", "Integer", "",1
        P: "FrontAxisSign", "int", "Integer", "",-1
        P: "CoordAxis", "int", "Integer", "",0
        P: "CoordAxisSign", "int", "Integer", "",1
        P: "UnitScaleFactor", "double", "Number", "",100
    }
}
Definitions: {
    Version: 100
    Count: 2
    ObjectType: "Geometry" { Count: 1 }
    ObjectType: "Model" { Count: 1 }
}
Objects: {
    Geometry: 1001, "Geometry::water_surface_trial", "Mesh" {
        GeometryVersion: 124
        Vertices: *%d { a: %s }
        PolygonVertexIndex: *%d { a: %s }
        LayerElementUV: 0 {
            Version: 101
            Name: "UVMap"
            MappingInformationType: "ByVertice"
            ReferenceInformationType: "Direct"
            UV: *%d { a: %s }
        }
        Layer: 0 {
            Version: 100
            LayerElement: {
                Type: "LayerElementUV"
                TypedIndex: 0
            }
        }
    }
    Model: 1002, "Model::water_surface_trial", "Mesh" {
        Version: 232
        Properties70: {
            P: "Lcl Translation", "Lcl Translation", "", "A",0,0,0
            P: "Lcl Rotation", "Lcl Rotation", "", "A",0,0,0
            P: "Lcl Scaling", "Lcl Scaling", "", "A",1,1,1
        }
        Shading: T
        Culling: "CullingOff"
    }
}
Connections: {
    C: "OO",1001,1002
    C: "OO",1002,0
}
''' % (len(vertices)*3, values(vertices), len(triangles)*3,
       values(indices), len(uv)*2, values(uv)), encoding='utf-8')
