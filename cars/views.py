from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum, Avg
from decimal import Decimal
from .models import Cars
from .forms import CarForm, FuelForm, ServiceForm, TyreForm

# 1. GARAŻ (Lista aut)
class GarageView(LoginRequiredMixin, View):
    def get(self, request):
        cars = Cars.objects.filter(user=request.user)
        return render(request, 'cars/garage.html', {'cars': cars})

# 2. DODAWANIE AUTA
class AddCarView(LoginRequiredMixin, View):
    def get(self, request):
        form = CarForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Samochód'})

    def post(self, request):
        form = CarForm(request.POST)
        if form.is_valid():
            car = form.save(commit=False)
            car.user = request.user
            car.save()
            return redirect('cars:garage')
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Samochód'})

# 3. KOKPIT (Główny widok ze szczegółami)
class CarDashboardView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        
        # Pobieranie danych
        fuel_logs = car.fuel_consumptions.all().order_by('-date')
        services = car.services.all().order_by('-date')
        tyres = car.tyres.all().order_by('-purchase_date')

        # Statystyki
        total_fuel = fuel_logs.aggregate(Sum('price'))['price__sum'] or 0
        total_service = services.aggregate(Sum('cost'))['cost__sum'] or 0
        total_tyres = tyres.aggregate(Sum('price'))['price__sum'] or 0
        total_cost = total_fuel + total_service + total_tyres + car.price
        
        avg_consumption = fuel_logs.aggregate(Avg('consumption'))['consumption__avg'] or 0

        context = {
            'car': car,
            'fuel_logs': fuel_logs,
            'services': services,
            'tyres': tyres,
            'total_cost': total_cost,
            'avg_consumption': avg_consumption,
        }
        return render(request, 'cars/dashboard.html', context)

# 4. DODAWANIE WPISÓW (Paliwo, Serwis, Opony)
# Przykład dla Paliwa (resztę robi się analogicznie)
class AddFuelView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = FuelForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Tankowanie: {car.brand}'})

    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = FuelForm(request.POST)
        if form.is_valid():
            dist = form.cleaned_data['odometer'] - car.odometer
            form.instance.consumption = (form.cleaned_data['liters'] * Decimal(100) / Decimal(dist)) if dist > 0 else 0
            log = form.save(commit=False)
            log.car = car
            # Opcjonalnie: Aktualizuj przebieg auta przy tankowaniu
            if log.odometer > car.odometer:
                car.odometer = log.odometer
                car.save()
            log.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Tankowanie'})

# 5. USUWANIE AUTA
class DeleteCarView(LoginRequiredMixin, View):
    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        car.delete()
        return redirect('cars:garage')

# 6. EDYTOWANIE AUTA
class EditCarView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = CarForm(instance=car)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Edytuj Samochód: {car.brand}'})

    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = CarForm(request.POST, instance=car)
        if form.is_valid():
            form.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Edytuj Samochód: {car.brand}'})

# 7. DODAWANIE SERWISU
class AddServiceView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = ServiceForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Dodaj Serwis: {car.brand}'})

    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = ServiceForm(request.POST)
        if form.is_valid():
            service = form.save(commit=False)
            service.car = car
            service.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Serwis'})
    
# 8. EDYTOWANIE SERWISU
class EditServiceView(LoginRequiredMixin, View):
    def get(self, request, car_id, service_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        service = get_object_or_404(car.services, id=service_id)
        form = ServiceForm(instance=service)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Edytuj Serwis: {car.brand}'})

    def post(self, request, car_id, service_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        service = get_object_or_404(car.services, id=service_id)
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Edytuj Serwis'})

# 9. USUWANIE SERWISU
class DeleteServiceView(LoginRequiredMixin, View):
    def post(self, request, car_id, service_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        service = get_object_or_404(car.services, id=service_id)
        service.delete()
        return redirect('cars:dashboard', car_id=car.id)

# 10. DODAWANIE OPON
class AddTyresView(LoginRequiredMixin, View):
    def get(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = TyreForm()
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Dodaj Opony: {car.brand}'})

    def post(self, request, car_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        form = TyreForm(request.POST)
        if form.is_valid():
            tyre = form.save(commit=False)
            tyre.car = car
            tyre.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Dodaj Opony'})

# 11. EDYTOWANIE OPON
class EditTyresView(LoginRequiredMixin, View):
    def get(self, request, car_id, tyre_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        tyre = get_object_or_404(car.tyres, id=tyre_id)
        form = TyreForm(instance=tyre)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Edytuj Opony: {car.brand}'})

    def post(self, request, car_id, tyre_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        tyre = get_object_or_404(car.tyres, id=tyre_id)
        form = TyreForm(request.POST, instance=tyre)
        if form.is_valid():
            form.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Edytuj Opony'})

# 12. USUWANIE OPON
class DeleteTyresView(LoginRequiredMixin, View):
    def post(self, request, car_id, tyre_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        tyre = get_object_or_404(car.tyres, id=tyre_id)
        tyre.delete()
        return redirect('cars:dashboard', car_id=car.id)

# 13. EDYTOWANIE WPISU O PALIWIE
class EditFuelView(LoginRequiredMixin, View):
    def get(self, request, car_id, fuel_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        fuel_log = get_object_or_404(car.fuel_consumptions, id=fuel_id)
        form = FuelForm(instance=fuel_log)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': f'Edytuj Tankowanie: {car.brand}'})

    def post(self, request, car_id, fuel_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        fuel_log = get_object_or_404(car.fuel_consumptions, id=fuel_id)
        form = FuelForm(request.POST, instance=fuel_log)
        if form.is_valid():
            dist = form.cleaned_data['odometer'] - car.odometer
            form.instance.consumption = (form.cleaned_data['liters'] * Decimal(100) / Decimal(dist)) if dist > 0 else 0
            log = form.save(commit=False)
            log.car = car
            # Opcjonalnie: Aktualizuj przebieg auta przy edycji tankowania
            if log.odometer > car.odometer:
                car.odometer = log.odometer
                car.save()
            log.save()
            return redirect('cars:dashboard', car_id=car.id)
        return render(request, 'cars/form_generic.html', {'form': form, 'title': 'Edytuj Tankowanie'})

# 14. USUWANIE WPISU O PALIWIE
class DeleteFuelView(LoginRequiredMixin, View):
    def post(self, request, car_id, fuel_id):
        car = get_object_or_404(Cars, id=car_id, user=request.user)
        fuel_log = get_object_or_404(car.fuel_consumptions, id=fuel_id)
        fuel_log.delete()
        return redirect('cars:dashboard', car_id=car.id)
