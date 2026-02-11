"""Test charger preference constraint."""
import unittest
from datetime import datetime, timedelta
from src.constraints.charger_preference import ChargerPreferenceConstraint
from src.models.vehicle import Vehicle
from src.models.route import Route


class TestChargerPreferenceConstraint(unittest.TestCase):
    """Test cases for charger preference constraint."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create test vehicles with different IDs
        self.vehicle1 = Vehicle(
            vehicle_id=101,
            site_id=10,
            active=True,
            VOR=False,
            charge_power_ac=11.0,
            charge_power_dc=50.0,
            battery_capacity=100.0,
            efficiency_kwh_mile=1.5,
            telematic_label="V101"
        )
        self.vehicle1.estimated_soc = 0.8
        self.vehicle1.available_time = datetime(2026, 2, 11, 4, 0, 0)
        
        self.vehicle2 = Vehicle(
            vehicle_id=102,
            site_id=10,
            active=True,
            VOR=False,
            charge_power_ac=11.0,
            charge_power_dc=50.0,
            battery_capacity=100.0,
            efficiency_kwh_mile=1.5,
            telematic_label="V102"
        )
        self.vehicle2.estimated_soc = 0.8
        self.vehicle2.available_time = datetime(2026, 2, 11, 4, 0, 0)
        
        self.vehicle3 = Vehicle(
            vehicle_id=103,
            site_id=10,
            active=True,
            VOR=False,
            charge_power_ac=11.0,
            charge_power_dc=50.0,
            battery_capacity=100.0,
            efficiency_kwh_mile=1.5,
            telematic_label="V103"
        )
        self.vehicle3.estimated_soc = 0.8
        self.vehicle3.available_time = datetime(2026, 2, 11, 4, 0, 0)
        
        # Create test routes at different times
        self.route1 = Route(  # First leaving - 5 AM
            route_id=201,
            site_id=10,
            vehicle_id=None,
            route_status='N',
            route_alias='R201',
            plan_start_date_time=datetime(2026, 2, 11, 5, 0, 0),
            plan_end_date_time=datetime(2026, 2, 11, 8, 0, 0),
            plan_mileage=50.0,
            n_orders=10
        )
        
        self.route2 = Route(  # Second leaving - 6 AM
            route_id=202,
            site_id=10,
            vehicle_id=None,
            route_status='N',
            route_alias='R202',
            plan_start_date_time=datetime(2026, 2, 11, 6, 0, 0),
            plan_end_date_time=datetime(2026, 2, 11, 9, 0, 0),
            plan_mileage=40.0,
            n_orders=8
        )
        
        self.route3 = Route(  # Third leaving - 6:30 AM
            route_id=203,
            site_id=10,
            vehicle_id=None,
            route_status='N',
            route_alias='R203',
            plan_start_date_time=datetime(2026, 2, 11, 6, 30, 0),
            plan_end_date_time=datetime(2026, 2, 11, 9, 30, 0),
            plan_mileage=45.0,
            n_orders=9
        )
        
        self.route_late = Route(  # Outside window - 10 AM
            route_id=204,
            site_id=10,
            vehicle_id=None,
            route_status='N',
            route_alias='R204',
            plan_start_date_time=datetime(2026, 2, 11, 10, 0, 0),
            plan_end_date_time=datetime(2026, 2, 11, 13, 0, 0),
            plan_mileage=40.0,
            n_orders=8
        )
        
        self.all_routes = [self.route1, self.route2, self.route3, self.route_late]
        self.all_vehicles = [self.vehicle1, self.vehicle2, self.vehicle3]
    
    def test_disabled_constraint(self):
        """Test that disabled constraint returns 0."""
        config = {
            'enabled': False,
            'params': {}
        }
        constraint = ChargerPreferenceConstraint(config)
        
        cost = constraint.evaluate(self.vehicle1, [self.route1])
        self.assertEqual(cost, 0.0)
    
    def test_highest_cost_vehicle_gets_first_route(self):
        """Test that vehicle with highest charger cost gets bonus for first leaving route."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5","86":"3","85":"1"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        # Vehicle rankings: V1=5 (rank 0), V2=3 (rank 1), V3=1 (rank 2)
        vehicle_charger_map = {
            101: '87',  # Highest cost (5)
            102: '86',  # Medium cost (3)
            103: '85'   # Lowest cost (1)
        }
        
        # Vehicle 1 (highest cost) evaluating first route (position 0)
        cost = constraint.evaluate(
            self.vehicle1, 
            [self.route1],  # First leaving route
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Vehicle rank (0) matches route position (0), should get cost 5
        self.assertEqual(cost, 5.0)
    
    def test_second_vehicle_gets_second_route(self):
        """Test that vehicle with second-highest cost gets bonus for second leaving route."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5","86":"3","85":"1"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        vehicle_charger_map = {
            101: '87',  # Rank 0 (cost 5)
            102: '86',  # Rank 1 (cost 3)
            103: '85'   # Rank 2 (cost 1)
        }
        
        # Vehicle 2 (rank 1) evaluating second route (position 1)
        cost = constraint.evaluate(
            self.vehicle2,
            [self.route2],  # Second leaving route
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Vehicle rank (1) matches route position (1), should get cost 3
        self.assertEqual(cost, 3.0)
    
    def test_mismatched_rank_gets_no_bonus(self):
        """Test that vehicle rank not matching route position gets no bonus."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5","86":"3","85":"1"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        vehicle_charger_map = {
            101: '87',  # Rank 0
            102: '86',  # Rank 1
            103: '85'   # Rank 2
        }
        
        # Vehicle 1 (rank 0) evaluating second route (position 1) - mismatch
        cost = constraint.evaluate(
            self.vehicle1,
            [self.route2],
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Rank 0 != position 1, no bonus
        self.assertEqual(cost, 0.0)
    
    def test_disc_charger_penalty(self):
        """Test that disconnected charger gets negative cost when matched."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"3","86":"1","DISC":"-5"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        # Vehicle with DISC is lowest (rank 2)
        vehicle_charger_map = {
            101: '87',   # Rank 0 (cost 3)
            102: '86',   # Rank 1 (cost 1)
            103: None    # Rank 2 (cost -5, DISC)
        }
        
        # Vehicle 3 (DISC, rank 2) evaluating third route (position 2)
        cost = constraint.evaluate(
            self.vehicle3,
            [self.route3],
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Rank 2 matches position 2, gets penalty -5
        self.assertEqual(cost, -5.0)
    
    def test_outside_time_window_no_cost(self):
        """Test that routes outside time window get no cost."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        vehicle_charger_map = {101: '87', 102: '86', 103: '85'}
        
        # Route at 10 AM - outside window
        cost = constraint.evaluate(
            self.vehicle1,
            [self.route_late],
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        self.assertEqual(cost, 0.0)
    
    def test_apply_to_all_routes(self):
        """Test apply_to_position='all' applies to all matching routes."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5","86":"3","85":"1"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'all'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        vehicle_charger_map = {
            101: '87',  # Rank 0 (cost 5)
            102: '86',  # Rank 1 (cost 3)
            103: '85'   # Rank 2 (cost 1)
        }
        
        # Vehicle 1 (rank 0) with routes at positions 0 and 1
        # Only route1 (position 0) should match rank 0
        cost = constraint.evaluate(
            self.vehicle1,
            [self.route1, self.route2],
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Only route1 matches (position 0 = rank 0), gets 5
        self.assertEqual(cost, 5.0)
    
    def test_unmapped_charger_gets_zero(self):
        """Test that unmapped chargers get cost 0 and no bonus."""
        config = {
            'enabled': True,
            'params': {
                'charger_preference_map': '{"87":"5","86":"3"}',
                'time_window_start': '4',
                'time_window_end': '8',
                'apply_to_position': 'first'
            }
        }
        constraint = ChargerPreferenceConstraint(config)
        
        # Vehicle 3 has unmapped charger (cost 0)
        vehicle_charger_map = {
            101: '87',  # Rank 0 (cost 5)
            102: '86',  # Rank 1 (cost 3)
            103: '99'   # Unmapped, cost 0 (won't participate)
        }
        
        cost = constraint.evaluate(
            self.vehicle3,
            [self.route1],
            vehicle_charger_map=vehicle_charger_map,
            all_routes=self.all_routes,
            all_vehicles=self.all_vehicles
        )
        
        # Vehicle with cost 0 doesn't participate
        self.assertEqual(cost, 0.0)
    
    def test_soft_constraint(self):
        """Test that charger preference is a soft constraint."""
        config = {'enabled': True, 'params': {}}
        constraint = ChargerPreferenceConstraint(config)
        
        self.assertFalse(constraint.is_hard_constraint())


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
