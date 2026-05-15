from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from cars.forms import FuelForm, TyreForm
from cars.models import Cars, CarFuelConsumption, CarService, CarServicePart, CarTyres, CarTyreUsage
from cars.views import annotate_fuel_distances


User = get_user_model()


class CarServiceViewsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="driver", password="pass12345")
        self.client.login(username="driver", password="pass12345")
        self.car = Cars.objects.create(
            user=self.user,
            brand="Toyota",
            model="Corolla",
            year=2020,
            odometer=84500,
            fuel_type="Benzyna",
            price="72000.00",
        )

    def test_add_service_saves_workshop_and_parts(self):
        response = self.client.post(
            reverse("cars:add_service", args=[self.car.id]),
            {
                "date": "2026-04-01",
                "service_type": 'Wymiana oleju',
                "workshop_name": "Auto Serwis Premium",
                "description": "Wymiana oleju silnikowego i filtrow.",
                "cost": "420.50",
                "parts-TOTAL_FORMS": "2",
                "parts-INITIAL_FORMS": "0",
                "parts-MIN_NUM_FORMS": "0",
                "parts-MAX_NUM_FORMS": "1000",
                "parts-0-name": "Olej 5W30",
                "parts-0-price": "189.99",
                "parts-1-name": "",
                "parts-1-price": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        service = CarService.objects.get(car=self.car, service_type="Wymiana oleju")
        self.assertEqual(service.workshop_name, "Auto Serwis Premium")
        self.assertEqual(service.description, "Wymiana oleju silnikowego i filtrow.")
        self.assertEqual(float(service.cost), 420.50)
        self.assertEqual(service.parts.count(), 1)
        self.assertEqual(service.parts.first().name, "Olej 5W30")

    def test_edit_service_can_replace_parts_list(self):
        service = CarService.objects.create(
            car=self.car,
            date=date(2026, 3, 15),
            service_type="Rozrzad",
            workshop_name="Stary warsztat",
            description="Pierwotny opis",
            cost="1800.00",
        )
        part = CarServicePart.objects.create(service=service, name="Pasek", price="350.00")

        response = self.client.post(
            reverse("cars:edit_service", args=[self.car.id, service.id]),
            {
                "date": "2026-03-20",
                "service_type": "Rozrzad i pompa wody",
                "workshop_name": "Nowy warsztat",
                "description": "Rozszerzony zakres naprawy.",
                "cost": "2150.00",
                "parts-TOTAL_FORMS": "2",
                "parts-INITIAL_FORMS": "1",
                "parts-MIN_NUM_FORMS": "0",
                "parts-MAX_NUM_FORMS": "1000",
                "parts-0-id": str(part.id),
                "parts-0-name": "Pasek rozrzadu",
                "parts-0-price": "420.00",
                "parts-0-DELETE": "on",
                "parts-1-name": "Pompa wody",
                "parts-1-price": "260.00",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        service.refresh_from_db()
        self.assertEqual(service.service_type, "Rozrzad i pompa wody")
        self.assertEqual(service.workshop_name, "Nowy warsztat")
        self.assertEqual(float(service.cost), 2150.00)
        self.assertFalse(service.parts.filter(id=part.id).exists())
        self.assertEqual(service.parts.count(), 1)
        self.assertEqual(service.parts.first().name, "Pompa wody")

    def test_service_history_pdf_returns_pdf_file(self):
        service = CarService.objects.create(
            car=self.car,
            date=date(2026, 2, 10),
            service_type="Hamulce i zawieszenie",
            workshop_name="Moto Klinika Łódź",
            description="Wymiana tarcz, klocków, łożysk oraz płynu hamulcowego.",
            cost="960.00",
        )
        CarServicePart.objects.create(
            service=service,
            name="Śruby mocujące wahacza przedniego z osłoną przeciwpyłową",
            price="500.00",
        )
        CarServicePart.objects.create(service=service, name="Płyn hamulcowy", price="180.00")

        response = self.client.get(reverse("cars:service_history_pdf", args=[self.car.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertNotIn(b"/Subtype /Image", response.content)
        self.assertIn(b"/ToUnicode", response.content)

    def test_fuel_consumption_is_calculated_from_previous_refuel(self):
        first_response = self.client.post(
            reverse("cars:add_fuel", args=[self.car.id]),
            {
                "date": "2026-04-01",
                "fuel_station": "Orlen",
                "liters": "35",
                "price": "245",
                "odometer": "85000",
            },
            follow=True,
        )
        second_response = self.client.post(
            reverse("cars:add_fuel", args=[self.car.id]),
            {
                "date": "2026-04-10",
                "fuel_station": "BP",
                "liters": "40",
                "price": "280",
                "odometer": "85500",
            },
            follow=True,
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)

        first_log = CarFuelConsumption.objects.get(car=self.car, fuel_station="Orlen")
        second_log = CarFuelConsumption.objects.get(car=self.car, fuel_station="BP")

        self.assertIsNone(first_log.consumption)
        self.assertEqual(float(second_log.consumption), 8.0)
        self.assertEqual(float(second_log.price_per_liter), 7.0)

    def test_fuel_form_does_not_expose_price_per_liter(self):
        self.assertNotIn("price_per_liter", FuelForm().fields)

    def test_fuel_logs_are_annotated_with_distance_since_last_refuel(self):
        first_log = CarFuelConsumption.objects.create(
            car=self.car,
            date=date(2026, 4, 1),
            fuel_station="Orlen",
            liters="35",
            price="245",
            odometer=85000,
        )
        second_log = CarFuelConsumption.objects.create(
            car=self.car,
            date=date(2026, 4, 10),
            fuel_station="BP",
            liters="40",
            price="280",
            odometer=85500,
        )

        logs = annotate_fuel_distances([second_log, first_log])

        self.assertIsNone(first_log.distance_since_last_refuel)
        self.assertEqual(second_log.distance_since_last_refuel, 500)
        self.assertEqual(logs, [second_log, first_log])

    def test_add_tyres_only_registers_purchased_set(self):
        response = self.client.post(
            reverse("cars:add_tyres", args=[self.car.id]),
            {
                "brand": "Michelin",
                "width": "205",
                "aspect_ratio": "55",
                "diameter": "16",
                "quantity": "4",
                "purchase_date": "2026-03-01",
                "price": "1600.00",
                "is_winter": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        tyre = CarTyres.objects.get(car=self.car, brand="Michelin")
        self.assertEqual(tyre.purchase_date, date(2026, 3, 1))
        self.assertIsNone(tyre.odometer)
        self.assertEqual(tyre.usage_periods.count(), 0)
        self.assertFalse(tyre.is_mounted)
        self.assertIsNone(tyre.total_driven_distance)

    def test_tyre_form_does_not_expose_purchase_odometer(self):
        self.assertNotIn("odometer", TyreForm().fields)

    def test_tyre_usage_tracks_mount_removal_and_distance(self):
        tyre = CarTyres.objects.create(
            car=self.car,
            brand="Michelin",
            width=205,
            aspect_ratio=55,
            diameter=16,
            quantity=4,
            purchase_date=date(2026, 3, 1),
            price="1600.00",
            odometer=84500,
            is_winter=False,
        )

        response = self.client.post(
            reverse("cars:add_tyre_usage", args=[self.car.id, tyre.id]),
            {
                "mounted_date": "2026-03-15",
                "mounted_odometer": "85000",
                "removed_date": "2026-10-20",
                "removed_odometer": "96500",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        usage = CarTyreUsage.objects.get(tyre=tyre)
        tyre.refresh_from_db()
        self.assertEqual(usage.mounted_date, date(2026, 3, 15))
        self.assertEqual(usage.mounted_odometer, 85000)
        self.assertEqual(usage.removed_date, date(2026, 10, 20))
        self.assertEqual(usage.removed_odometer, 96500)
        self.assertEqual(usage.driven_distance, 11500)
        self.assertEqual(tyre.total_driven_distance, 11500)
        self.assertFalse(tyre.is_mounted)

    def test_tyres_removal_odometer_must_not_be_lower_than_mount_odometer(self):
        tyre = CarTyres.objects.create(
            car=self.car,
            brand="Continental",
            width=205,
            aspect_ratio=55,
            diameter=16,
            quantity=4,
            purchase_date=date(2026, 3, 1),
            price="1500.00",
            odometer=84500,
            is_winter=False,
        )

        response = self.client.post(
            reverse("cars:add_tyre_usage", args=[self.car.id, tyre.id]),
            {
                "mounted_date": "2026-03-15",
                "mounted_odometer": "85000",
                "removed_date": "2026-10-20",
                "removed_odometer": "84000",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Przebieg przy zdjęciu nie może być mniejszy")
        self.assertFalse(CarTyreUsage.objects.filter(tyre=tyre).exists())
