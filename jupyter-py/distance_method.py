from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import math
import glob
import os
import uuid
import itertools

import pandas as pd
import numpy as np
import datetime as dt

import backtrader as bt
import backtrader.feeds as btfeeds
import backtrader.indicators as btind


class Metrics(bt.Analyzer):
    params = dict(
        lookback=10,
    )

    def __init__(self):
        super(Metrics, self).__init__()

        self.pv = []
        self.status = []
        self.returns = None

        ###############################################################
        #  Trade statistics                                           #
        ###############################################################

        # number of resolved trades
        self.n_trades = 0

        # a trade (long or short) is not resolved if the position hasn't changed until the end of backtest 
        self.n_resolved_trades = 0
        self.n_unresolved_trades = 0

        # average holding period of resolved trades, -1 if there aren't any
        self.avg_holding_period = -1

        # length of unresolved trade, -1 if there aren't any
        self.len_unresolved_trade = -1

    def start(self):
        pass

    def next(self):
        if min(len(self.strategy.data0), len(self.strategy.data1)) >= self.p.lookback:
            self.pv.append(self.strategy.broker.getvalue())
            self.status.append(self.strategy.status)
            
    def stop(self):
        # convert lists to series
        self.pv = pd.Series(self.pv)
        
        # calculate returns
        self.returns = self.pv.diff()[1:]
        
        # calculate number of trades
        self.compute_trade_statistics()
        
    def compute_trade_statistics(self):
        _n = 0
        _mean = 0
        _counter = 0
        _curstate = 0
        
        for i, status in enumerate(self.status):
            if _curstate == 0:
                if status == 0:
                    continue
                else:
                    # entered position
                    _curstate = status
                    _counter = 1
                    
            else:
                if status == 0 or status != _curstate:
                    # changed position
                    _mean = (_n * _mean + _counter) / float(_n + 1)
                    _n += 1
                    _counter = 1
                    _curstate = status
                    
                elif status == _curstate:
                    _counter += 1
        
        self.n_resolved_trades = _n 
        self.n_unresolved_trades = 0 if (_curstate == 0) else 1
        self.n_trades = self.n_resolved_trades + self.n_unresolved_trades
        self.avg_holding_period = _mean if (self.n_resolved_trades > 0) else -1
        self.len_unresolved_trade = _counter if (self.n_unresolved_trades == 1) else -1
        
    def portfolio_value(self):
        return self.pv

    def returns_std(self):
        return self.returns.std()

class DistanceStrategy(bt.Strategy):
    params = dict (
        lookback=84,
        max_lookback=84,
        enter_threshold_size = 2,
        exit_threshold_size = 0.5,
        loss_limit = -0.015,
        print_bar = True,
        print_msg = False,
        print_transaction = False,
    )

    def __init__(self):
        self.orderid = None
        
        # Strategy params
        self.lookback = self.p.lookback
        self.max_lookback = self.p.max_lookback
        self.enter_threshold_size = self.p.enter_threshold_size
        self.exit_threshold_size = self.p.exit_threshold_size
        self.exposure = 200000
        
        # Parameters for printing
        self.print_bar = self.p.print_bar
        self.print_msg = self.p.print_msg
        self.print_transaction = self.p.print_transaction
            
        # signals
        self.resid_mean = None
        self.resid_std = None
        self.spread = None
        
        # temporary variables to keep track of trades
        self.status = 0
        self.qty0 = 0
        self.qty1 = 0
        self.initial_price_data0 = None
        self.initial_price_data1 = None
        self.initial_cash = None
        self.initial_long_pv = None
        self.initial_short_pv = None
        self.upper_limit = None
        self.lower_limit = None
        self.up_medium = None
        self.low_medium = None
        
    def log(self, txt, dt=None):        
        dt = dt or self.data.datetime[0]
        dt = bt.num2date(dt)
        print('%s, %s' % (dt.isoformat(), txt))

    def notify_order(self, order):
        if order.status in [bt.Order.Submitted, bt.Order.Accepted]:
            return  # Await further notifications

        if order.status == order.Completed:
            if order.isbuy():
                buytxt = 'BUY COMPLETE, %.2f' % order.executed.price
                
                if self.print_transaction:
                    self.log(buytxt, order.executed.dt)
                    
                self.incur_commission(order.executed.price, order.executed.size)
            else:
                selltxt = 'SELL COMPLETE, %.2f' % order.executed.price
                
                if self.print_transaction:
                    self.log(selltxt, order.executed.dt)
                    
                self.incur_commission(order.executed.price, order.executed.size)

        elif order.status in [order.Expired, order.Canceled, order.Margin]:
            if self.print_transaction:
                self.log('%s ,' % order.Status[order.status])

        # Allow new orders
        self.orderid = None

    def next(self):
        if min(len(self.data0), len(self.data1)) < self.max_lookback:
            return
        
        if self.orderid:
            return  # if an order is active, no new orders are allowed
        
        ##################################################################################################
        # COMPUTE UPPER AND LOWER LIMITS                                                                 #
        ##################################################################################################
        Y = pd.Series(self.data0.get(size=self.lookback, ago=0))
        X = pd.Series(self.data1.get(size=self.lookback, ago=0))
        
        self.spread = (self.data0[0] - self.data1[0])
        
        if self.status == 0:
            self.spread_mean = (Y - X).mean()
            self.spread_std = (Y - X).std()

            self.upper_limit = self.spread_mean + self.enter_threshold_size * self.spread_std
            self.lower_limit = self.spread_mean - self.enter_threshold_size * self.spread_std
            self.up_medium = self.spread_mean + self.exit_threshold_size * self.spread_std
            self.low_medium = self.spread_mean - self.exit_threshold_size * self.spread_std
    
        ##################################################################################################
        # STRATEGY LOGIC                                                                                 #
        ##################################################################################################
        if self.status == 0:
            # "NO position" status
            
            if self.spread > self.upper_limit:
                self.short_spread()
            elif self.spread < self.lower_limit:
                self.long_spread()
     
        elif self.status == 1:
            # "SHORT the spread" status
            # short data0, long data1
            
            if self.spread < self.lower_limit:
                self.long_spread()
                
            elif self.spread < self.up_medium:
                self.exit_spread()
            
            else:
                long_pv = self.long_portfolio_value(self.data1.close, self.qty1)
                short_pv = self.short_portfolio_value(self.initial_price_data0, self.data0.close, self.qty0)
                net_gain_long = long_pv - self.initial_long_pv
                net_gain_short = short_pv - self.initial_short_pv

                return_of_current_trade = (net_gain_long + net_gain_short) / self.initial_cash

                # if losing too much money, exit
                if return_of_current_trade < self.p.loss_limit or short_pv <= 0:
                    self.exit_spread()
        
        elif self.status == 2:
            # "LONG the spread" status
            # short data1, long data0
            
            if self.spread > self.upper_limit:
                self.short_spread()
                
            elif self.spread > self.low_medium:
                self.exit_spread()
            
            else:
                long_pv = self.long_portfolio_value(self.data0.close, self.qty0)
                short_pv = self.short_portfolio_value(self.initial_price_data1, self.data0.close, self.qty1)
                net_gain_long = long_pv - self.initial_long_pv
                net_gain_short = short_pv - self.initial_short_pv

                return_of_current_trade = (net_gain_long + net_gain_short) / self.initial_cash

                # if losing too much money, exit
                if return_of_current_trade < self.p.loss_limit or short_pv <= 0:
                    self.exit_spread()
    
    def long_portfolio_value(self, price, qty):
        return price * qty
        
    def short_portfolio_value(self, price_initial, price_final, qty):
        return qty * (1.5 * price_initial - price_final)
    
    def short_spread(self):
        x = int((2 * self.broker.getvalue() / 3.0) / (self.data0.close))  
        y = int((2 * self.broker.getvalue() / 3.0) / (self.data1.close))  

        # Placing the order
        self.sell(data=self.data0, size=(x + self.qty0))  # Place an order for buying y + qty2 shares
        self.buy(data=self.data1, size=(y + self.qty1))  # Place an order for selling x + qty1 shares

        # Updating the counters with new value
        self.qty0 = x  
        self.qty1 = y  
        
        # update flags
        self.status = 1
        
        # keep track of trade variables
        self.initial_cash = self.qty1 * self.data1.close + 0.5 * self.qty0 * self.data0.close
        self.initial_long_pv = self.long_portfolio_value(self.qty1, self.data1.close)
        self.initial_short_pv = 0.5 * self.data0.close * self.qty0
        self.initial_price_data0, self.initial_price_data1 = self.data0.close, self.data1.close
    
    def long_spread(self):
        # Calculating the number of shares for each stock
        x = int((2 * self.broker.getvalue() / 3.0) / (self.data0.close)) 
        y = int((2 * self.broker.getvalue() / 3.0) / (self.data1.close)) 
    

        # Place the order
        self.buy(data=self.data0, size=(x + self.qty0))  # Place an order for buying x + qty1 shares
        self.sell(data=self.data1, size=(y + self.qty1))  # Place an order for selling y + qty2 shares

        # Updating the counters with new value
        self.qty0 = x 
        self.qty1 = y 
        
        # update flags
        self.status = 2  
        
        # keep track of trade variables
        self.initial_cash = self.qty0 * self.data0.close + 0.5 * self.qty1 * self.data1.close
        self.initial_long_pv = self.long_portfolio_value(self.qty0, self.data0.close)
        self.initial_short_pv = 0.5 * self.data1.close * self.qty1
        self.initial_price_data0, self.initial_price_data1 = self.data0.close, self.data1.close
    
    def exit_spread(self):
        # Exit position
        self.close(self.data0)
        self.close(self.data1)
        
        # update counters
        self.qty0 = 0
        self.qty1 = 0
        
        # update flags
        self.status = 0
        self.initial_cash = None
        self.initial_long_pv, self.initial_short_pv = None, None
        self.initial_price_data0, self.initial_price_data1 = None, None
        
    def incur_commission(self, price, qty):
        qty = abs(qty)
        commission = min(max(1, 0.005*qty), 0.01*price*qty)
        self.broker.add_cash(-1*commission)
    
    def incur_borrow_cost(self, ):
        pass

    def stop(self):
        if self.print_bar:
            print("-", end="")
        
        if self.print_msg:
            print('==================================================')
            print('Starting Value: %.2f' % self.broker.startingcash)
            print('Ending   Value: %.2f' % self.broker.getvalue())
            print('==================================================')