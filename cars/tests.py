from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from cars.models import Cars, CarService, CarServicePart


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
            service_type="Hamulce",
            workshop_name="Moto Klinika",
            description="Wymiana tarcz i klockow hamulcowych.",
            cost="960.00",
        )
        CarServicePart.objects.create(service=service, name="Tarcze", price="500.00")
        CarServicePart.objects.create(service=service, name="Klocki", price="180.00")

        response = self.client.get(reverse("cars:service_history_pdf", args=[self.car.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))
