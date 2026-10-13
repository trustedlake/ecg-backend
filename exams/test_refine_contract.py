from django.test import SimpleTestCase
from .views import PaintRequestSerializer

class RefinePayloadTests(SimpleTestCase):
    def test_valid_stroke(self):
        payload={"coordinate_system":"normalized_image_v1","lead":"II","strokes":[{"lead":"II","mode":"include","points":[{"x":0.1,"y":0.5}]}]}
        assert PaintRequestSerializer(data=payload).is_valid()
    def test_reject_outside_image(self):
        payload={"coordinate_system":"normalized_image_v1","lead":"II","strokes":[{"lead":"II","mode":"exclude","points":[{"x":1.2,"y":0.5}]}]}
        assert not PaintRequestSerializer(data=payload).is_valid()
    def test_cross_lead_not_allowed(self):
        payload={"coordinate_system":"normalized_image_v1","lead":"II","strokes":[{"lead":"V1","mode":"include","points":[{"x":0.1,"y":0.5}]}]}
        assert not PaintRequestSerializer(data=payload).is_valid()
