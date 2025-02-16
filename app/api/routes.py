# app/api/routes.py
import logging
import os
from datetime import datetime
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple, Union

from flask import Response, jsonify, render_template, request

from app import app
from app.core.delta_hedger import DeltaHedger
from app.models.position import Position
from app.services.ig_client import IGClient, IGAPIError
from config.settings import HEDGE_SETTINGS as _hedge_settings

# Type alias for Flask responses
ApiResponse = Union[Response, Tuple[Response, int]]
logger = logging.getLogger(__name__)

# Initialize clients with proper error handling
try:
    # Initialize
    ig_client = IGClient(
        api_key=os.getenv("IG_API_KEY"),
        username=os.getenv("IG_USERNAME"),
        password=os.getenv("IG_PASSWORD")
    )
    
    # Explicitly login after initialization
    if not ig_client.login():
        logger.critical("Failed to login to IG API")
        raise IGAPIError("Failed to login to IG API")
        
    hedger = DeltaHedger(ig_client)
except IGAPIError as e:
    logger.critical(f"IG API Error: {str(e)}")
    raise
except Exception as e:
    logger.critical(f"Failed to initialize clients: {str(e)}")
    raise


def validate_json_request() -> Optional[Dict]:
    """Validate JSON request data"""
    if not request.is_json:
        raise ValueError("Request must be JSON")
    data = request.get_json()
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON data structure")
    return data


@app.route("/")
def index() -> str:
    """Render main application page"""
    return render_template("index.html")

@app.route("/config")
def get_config():
    return jsonify({
        "apiBaseUrl": os.getenv("API_BASE_URL", "http://localhost:8000/api")
    })

@app.route("/api/monitor/start", methods=["POST"])
def start_monitoring() -> ApiResponse:
    """Start automated position monitoring and delta hedging"""
    try:
        data = validate_json_request()
        if not data:
            return jsonify({"error": "Invalid request data"}), HTTPStatus.BAD_REQUEST

        monitor_interval = float(
            data.get("interval", _hedge_settings["hedge_interval"])
        )
        delta_threshold = float(
            data.get("delta_threshold", _hedge_settings["delta_threshold"])
        )

        if monitor_interval <= 0:
            return (
                jsonify({"error": "Invalid monitoring interval"}),
                HTTPStatus.BAD_REQUEST,
            )
        if delta_threshold <= 0:
            return jsonify({"error": "Invalid delta threshold"}), HTTPStatus.BAD_REQUEST

        result = hedger.start_monitoring(
            interval=monitor_interval, delta_threshold=delta_threshold
        )

        logger.info(f"Started position monitoring: {result}")
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), HTTPStatus.BAD_REQUEST
    except Exception as e:
        logger.error(f"Error starting monitoring: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/positions", methods=["GET"])
def fetch_positions() -> ApiResponse:
    try:
        positions_response = ig_client.get_positions()
        if "error" in positions_response:
            return (
                jsonify({"error": positions_response["error"]}),
                HTTPStatus.BAD_REQUEST,
            )

        positions = positions_response.get("positions", [])
        if not positions:
            return (
                jsonify(
                    {
                        "positions": [],
                        "message": "No positions found",
                        "monitoring_status": hedger.get_monitoring_status(),
                    }
                ),
                HTTPStatus.OK,
            )

        positions_data = []
        total_delta = 0.0
        total_pnl = 0.0
        total_exposure = 0.0

        for position_data in positions:
            try:
                position = Position.from_dict(position_data)
                delta_info = hedger.calculate_position_delta(position)
                position_metrics = hedger.calculate_position_metrics(position)

                position_dict = position.to_dict()
                position_dict.update(
                    {
                        "delta": delta_info.get("delta", 0),
                        "needs_hedge": delta_info.get("needs_hedge", False),
                        "suggested_hedge": delta_info.get("suggested_hedge_size", 0),
                        "metrics": position_metrics,
                        "greeks": delta_info.get("greeks", {}),
                    }
                )

                positions_data.append(position_dict)
                total_delta += delta_info.get("delta", 0)
                total_pnl += position_metrics.get("pnl", 0)
                total_exposure += position_metrics.get("exposure", 0)
            except Exception as e:
                logger.error(f"Error processing position: {str(e)}")
                continue

        return jsonify(
            {
                "positions": positions_data,
                "portfolio_summary": {
                    "total_positions": len(positions),
                    "total_delta": round(total_delta, 4),
                    "total_pnl": round(total_pnl, 2),
                    "total_exposure": round(total_exposure, 2),
                    "monitoring_status": hedger.get_monitoring_status(),
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting positions: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/positions/<position_id>", methods=["GET"])
def get_position(position_id: str) -> ApiResponse:
    try:
        position = hedger.get_position(position_id)
        if not position:
            return jsonify({"error": "Position not found"}), HTTPStatus.NOT_FOUND

        if not position.epic or not isinstance(position.epic, str):
            return jsonify({"error": "Invalid epic value"}), HTTPStatus.BAD_REQUEST

        market_data = ig_client.get_market_data(position.epic)
        if not market_data:
            return (
                jsonify({"error": "Failed to fetch market data"}),
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

        delta_info = hedger.calculate_position_delta(position)
        metrics = hedger.calculate_position_metrics(position)
        greeks = hedger.calculator.calculate_greeks(
            S=market_data["price"],
            K=position.strike,
            T=position.time_to_expiry,
            sigma=market_data.get("volatility", 0.2),
            option_type=position.option_type,
        )

        return jsonify(
            {
                "position": position.to_dict(),
                "market_data": market_data,
                "analysis": {"delta": delta_info, "metrics": metrics, "greeks": greeks},
                "hedge_history": [h.to_dict() for h in position.hedge_history],
                "status": hedger.get_position_status(position_id),
            }
        )

    except Exception as e:
        logger.error(f"Error getting position {position_id}: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/hedge/<position_id>", methods=["POST"])
def hedge_position(position_id: str) -> ApiResponse:
    try:
        data = validate_json_request()
        if not data:
            return jsonify({"error": "Invalid request data"}), HTTPStatus.BAD_REQUEST

        force_hedge = bool(data.get("force", False))
        hedge_size = float(data["hedge_size"]) if "hedge_size" in data else None

        result = hedger.hedge_position(
            position_id=position_id, hedge_size=hedge_size, force_hedge=force_hedge
        )

        if "error" in result:
            return jsonify(result), HTTPStatus.BAD_REQUEST

        position = hedger.get_position(position_id)
        if position:
            market_data = ig_client.get_market_data(position.epic)
            result.update(
                {
                    "position": position.to_dict(),
                    "market_data": market_data,
                    "metrics": hedger.calculate_position_metrics(position),
                }
            )

        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), HTTPStatus.BAD_REQUEST
    except Exception as e:
        logger.error(f"Error hedging position {position_id}: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/hedge/status", methods=["GET"])
def get_hedge_status() -> ApiResponse:
    """Get hedging status for all positions"""
    try:
        positions_status = hedger.get_all_positions_status()
        monitoring_status = hedger.get_monitoring_status()

        positions_needing_hedge = sum(
            1 for p in positions_status.values() if p.get("needs_hedge", False)
        )
        total_exposure = sum(
            p.get("metrics", {}).get("exposure", 0) for p in positions_status.values()
        )

        return jsonify(
            {
                "positions_status": positions_status,
                "monitoring": monitoring_status,
                "summary": {
                    "total_positions": len(positions_status),
                    "positions_needing_hedge": positions_needing_hedge,
                    "total_exposure": total_exposure,
                    "last_update": datetime.now().isoformat(),
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting hedge status: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/settings", methods=["GET", "POST", "PUT"])
def handle_settings() -> ApiResponse:
    """Handle hedging settings"""
    try:
        if request.method in ["POST", "PUT"]:  # Handle both POST and PUT
            data = validate_json_request()
            if not data:
                return jsonify({"error": "Invalid request data"}), HTTPStatus.BAD_REQUEST
                
            validation_result = hedger.validate_settings(data)

            if "error" in validation_result:
                return jsonify(validation_result), HTTPStatus.BAD_REQUEST

            updated_settings = hedger.get_current_settings()
            return jsonify({
                "message": "Settings updated successfully",
                "settings": updated_settings,
            })

        # GET request
        current_settings = hedger.get_current_settings()
        monitoring_status = hedger.get_monitoring_status()

        return jsonify({
            "settings": current_settings, 
            "monitoring_status": monitoring_status
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), HTTPStatus.BAD_REQUEST
    except Exception as e:
        logger.error(f"Error handling settings: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR

@app.route("/api/analytics/<position_id>", methods=["GET"])
def get_position_analytics(position_id: str) -> ApiResponse:
    """Get detailed analytics for a position"""
    try:
        position = hedger.get_position(position_id)
        if not position:
            return jsonify({"error": "Position not found"}), HTTPStatus.NOT_FOUND

        market_data = ig_client.get_market_data(position.epic)  # type: ignore
        if not market_data:
            return (
                jsonify({"error": "Failed to fetch market data"}),
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

        delta_info = hedger.calculate_position_delta(position)
        metrics = hedger.calculate_position_metrics(position)
        greeks = hedger.calculator.calculate_greeks(
            S=market_data["price"],
            K=position.strike,
            T=position.time_to_expiry,
            sigma=market_data.get("volatility", 0.2),
            option_type=position.option_type,
        )

        return jsonify(
            {
                "position": position.to_dict(),
                "market_data": market_data,
                "greeks": greeks,
                "delta_info": delta_info,
                "metrics": metrics,
                "hedge_history": [h.to_dict() for h in position.hedge_history],
            }
        )

    except Exception as e:
        logger.error(f"Error getting analytics for position {position_id}: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR


@app.route("/api/hedge/all", methods=["POST"])
def hedge_all_positions() -> ApiResponse:
    """Hedge all positions with manual override support"""
    try:
        data = validate_json_request() or {}
        is_manual = data.get("manual", True)

        positions_status = hedger.get_all_positions_status()
        if "error" in positions_status:
            return jsonify(positions_status), HTTPStatus.BAD_REQUEST

        results = []
        for pos_id, status in positions_status.items():
            try:
                if is_manual or status.get("needs_hedge", False):
                    result = hedger.hedge_position(pos_id, force_hedge=True)
                    results.append(
                        {
                            "position_id": pos_id,
                            "result": result,
                            "manually_hedged": is_manual,
                        }
                    )
            except Exception as e:
                logger.error(f"Error hedging position {pos_id}: {str(e)}")
                results.append({"position_id": pos_id, "error": str(e)})

        if not results:
            return (
                jsonify(
                    {
                        "message": "No positions require hedging",
                        "positions_checked": len(positions_status),
                        "mode": "manual" if is_manual else "automatic",
                    }
                ),
                HTTPStatus.OK,
            )

        return jsonify(
            {
                "message": f"Hedged {len(results)} positions",
                "mode": "manual" if is_manual else "automatic",
                "results": results,
            }
        )

    except Exception as e:
        logger.error(f"Error hedging all positions: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR

'''
@app.route("/api/positions/sold", methods=["GET"])
def get_sold_positions() -> ApiResponse:
    """Get all sold positions"""
    try:
        result = hedger.get_sold_positions()
        if "error" in result:
            return jsonify(result), HTTPStatus.BAD_REQUEST

        if not result["positions"]:
            return (
                jsonify({"message": "No sold positions found", "positions": []}),
                HTTPStatus.OK,
            )

        return jsonify(result)

    except Exception as e:
        logger.error(f"Error getting sold positions: {str(e)}")
        return jsonify({"error": str(e)}), HTTPStatus.INTERNAL_SERVER_ERROR
'''