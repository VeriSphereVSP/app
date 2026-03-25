// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2025 Verisphere Ltd. All rights reserved.
//
// This contract is a COMMERCIAL SERVICE COMPONENT operated by Verisphere Ltd.
// It is NOT part of the VeriSphere open-source protocol (verisphere/core).
//
// The VeriSphere protocol is permissionless. Users may interact with protocol
// contracts (PostRegistry, StakeEngine, etc.) directly without this forwarder.
// This forwarder provides gasless meta-transactions as a convenience service
// and charges a percentage-based VSP fee for that service.
//
// License: Business Source License 1.1 (BUSL-1.1)
// Change Date: 2028-01-01
// Change License: MIT

pragma solidity ^0.8.20;

import "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @title VerisphereForwarder
/// @notice Trusted forwarder for gasless meta-transactions (ERC-2771) with
///         a percentage-based VSP relay fee.
///
///         Fee model:
///           - The relay inspects the forwarded calldata to determine the
///             economic value of the transaction (stake amount, posting fee, etc.)
///           - A percentage (feeBps, in basis points) is charged in VSP
///           - Fee is transferred from the user to the treasury address
///           - Fee collection requires the user to have approved this forwarder
///             for VSP spending (done via EIP-2612 permit in the relay flow)
///
///         This contract is operated by Verisphere Ltd. The protocol does not
///         require it. Users may call protocol contracts directly with their
///         own wallet and pay zero relay fee.
contract VerisphereForwarder is ERC2771Forwarder {

    // ── State ────────────────────────────────────────────────

    IERC20 public immutable vspToken;
    address public treasury;
    address public owner;
    uint256 public feeBps;          // Fee in basis points (50 = 0.5%)
    uint256 public minFeeWei;       // Minimum fee per tx (e.g. 0.001 VSP)
    bool public feeEnabled;

    // Known function selectors for value extraction
    bytes4 private constant SEL_CREATE_CLAIM = bytes4(keccak256("createClaim(string)"));
    bytes4 private constant SEL_CREATE_LINK  = bytes4(keccak256("createLink(uint256,uint256,bool)"));
    bytes4 private constant SEL_STAKE        = bytes4(keccak256("stake(uint256,uint8,uint256)"));
    bytes4 private constant SEL_WITHDRAW     = bytes4(keccak256("withdraw(uint256,uint8,uint256,bool)"));

    // ── Events ───────────────────────────────────────────────

    event FeeCollected(address indexed user, uint256 fee, uint256 txValue);
    event FeeConfigUpdated(uint256 feeBps, uint256 minFeeWei, bool enabled);
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event OwnerUpdated(address indexed oldOwner, address indexed newOwner);

    // ── Errors ───────────────────────────────────────────────

    error NotOwner();
    error ZeroAddress();

    // ── Constructor ──────────────────────────────────────────

    /// @param vspToken_   The VSP token contract address.
    /// @param treasury_   Address that receives relay fees.
    /// @param feeBps_     Fee in basis points (e.g. 50 = 0.5%).
    /// @param minFeeWei_  Minimum fee in VSP wei (e.g. 1e15 = 0.001 VSP).
    constructor(
        address vspToken_,
        address treasury_,
        uint256 feeBps_,
        uint256 minFeeWei_
    ) ERC2771Forwarder("VerisphereForwarder") {
        if (vspToken_ == address(0)) revert ZeroAddress();
        if (treasury_ == address(0)) revert ZeroAddress();
        vspToken = IERC20(vspToken_);
        treasury = treasury_;
        owner = msg.sender;
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = true;
    }

    // ── Modifiers ────────────────────────────────────────────

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ── Fee extraction ───────────────────────────────────────

    /// @dev Extract the economic value (in VSP wei) from the forwarded calldata.
    ///      Returns 0 if the operation type is unknown (fee will be minFeeWei).
    function _extractTxValue(bytes calldata data) internal pure returns (uint256) {
        if (data.length < 4) return 0;
        bytes4 sel = bytes4(data[:4]);

        if (sel == SEL_CREATE_CLAIM || sel == SEL_CREATE_LINK) {
            // Posting fee is 1 VSP (1e18). The actual fee is set by the
            // PostingFeePolicy contract, but 1e18 is the deployed default.
            // If the policy changes, update this or read it on-chain.
            return 1e18;
        }

        if (sel == SEL_STAKE) {
            // stake(uint256 postId, uint8 side, uint256 amount)
            // amount is the 3rd parameter, starting at byte 4 + 64 = 68
            if (data.length >= 100) {
                return uint256(bytes32(data[68:100]));
            }
            return 0;
        }

        if (sel == SEL_WITHDRAW) {
            // withdraw(uint256 postId, uint8 side, uint256 amount, bool lifo)
            // amount is the 3rd parameter, same offset
            if (data.length >= 100) {
                return uint256(bytes32(data[68:100]));
            }
            return 0;
        }

        return 0;
    }

    /// @dev Compute and collect the relay fee from the user.
    ///      Non-reverting: if fee collection fails (insufficient balance/allowance),
    ///      the meta-tx still proceeds. This ensures the forwarder remains usable
    ///      even if the fee mechanism has issues.
    function _collectFee(address user, bytes calldata innerData) internal {
        if (!feeEnabled || feeBps == 0) return;

        uint256 txValue = _extractTxValue(innerData);
        uint256 fee = (txValue * feeBps) / 10_000;
        if (fee < minFeeWei) fee = minFeeWei;

        // Non-reverting transfer — fee failure should not block the user's tx
        try vspToken.transferFrom(user, treasury, fee) {
            emit FeeCollected(user, fee, txValue);
        } catch {
            // Fee collection failed — log but proceed.
            // Common causes: insufficient allowance or balance.
        }
    }

    // ── Execute override ─────────────────────────────────────

    /// @notice Execute a meta-transaction, collecting a relay fee first.
    /// @dev Overrides OZ ERC2771Forwarder.execute. The ForwardRequestData struct
    ///      is defined in ERC2771Forwarder and contains:
    ///        from, to, value, gas, deadline, data, signature
    function execute(
        ForwardRequestData calldata request
    ) public payable override {
        // Collect fee from the user before forwarding
        _collectFee(request.from, request.data);

        // Delegate to OZ implementation (signature verification + forwarding)
        super.execute(request);
    }

    // ── Admin ────────────────────────────────────────────────

    function setFeeConfig(
        uint256 feeBps_,
        uint256 minFeeWei_,
        bool enabled_
    ) external onlyOwner {
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = enabled_;
        emit FeeConfigUpdated(feeBps_, minFeeWei_, enabled_);
    }

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        address old = treasury;
        treasury = treasury_;
        emit TreasuryUpdated(old, treasury_);
    }

    function setOwner(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        address old = owner;
        owner = newOwner;
        emit OwnerUpdated(old, newOwner);
    }
}
