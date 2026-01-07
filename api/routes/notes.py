"""
API routes for managing notes (fact-checks on promises).
"""

import json
from datetime import datetime, UTC
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from api.database import get_db, Note, Rating
from api.models import (
    CreateNoteRequest, NoteResponse, NoteStatus, ScoredNotesResponse
)


router = APIRouter(prefix="/notes", tags=["Notes"])


# =============================================================================
# Create Note
# =============================================================================


@router.post("/", response_model=NoteResponse, status_code=201)
def create_note(
    request: CreateNoteRequest,
    db: Session = Depends(get_db)
) -> NoteResponse:
    """
    Create a new fact-check note on a promise.
    
    Users can submit notes providing context, fact-checks, or evidence
    about political promises. Notes start with status "needs_more_ratings"
    and will be scored once they receive sufficient community ratings.
    """
    # Create the note
    note = Note(
        promise_id=request.promise_id,
        author_id=request.author_id,
        summary=request.summary,
        content=request.content,
        sources=json.dumps(request.sources),
        classification=request.classification.value,
        status=NoteStatus.NEEDS_MORE_RATINGS.value,
    )
    
    db.add(note)
    db.commit()
    db.refresh(note)
    
    return _note_to_response(note)


# =============================================================================
# Get Notes
# =============================================================================


@router.get("/by-author/{author_id}", response_model=List[NoteResponse])
def get_notes_by_author(
    author_id: int,
    status: Optional[NoteStatus] = None,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
    db: Session = Depends(get_db)
) -> List[NoteResponse]:
    """
    Get all notes created by a specific author.
    
    Returns notes with their current status and scoring information.
    Optionally filter by status.
    """
    query = db.query(Note).filter(Note.author_id == author_id)
    
    if status:
        query = query.filter(Note.status == status.value)
    
    # Order by creation date (newest first)
    query = query.order_by(Note.created_at.desc())
    
    notes = query.offset(offset).limit(limit).all()
    
    return [_note_to_response(n) for n in notes]


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(
    note_id: int,
    db: Session = Depends(get_db)
) -> NoteResponse:
    """Get a single note by ID."""
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    return _note_to_response(note)


@router.get("/promise/{promise_id}", response_model=List[NoteResponse])
def get_notes_for_promise(
    promise_id: int,
    status: Optional[NoteStatus] = None,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
    db: Session = Depends(get_db)
) -> List[NoteResponse]:
    """
    Get all notes for a specific promise.
    
    Optionally filter by status to get only helpful notes or those
    needing more ratings.
    """
    query = db.query(Note).filter(Note.promise_id == promise_id)
    
    if status:
        query = query.filter(Note.status == status.value)
    
    # Order by helpfulness score (helpful notes first), then by date
    query = query.order_by(
        Note.note_intercept.desc().nullslast(),
        Note.created_at.desc()
    )
    
    notes = query.offset(offset).limit(limit).all()
    
    return [_note_to_response(n) for n in notes]


@router.get("/promise/{promise_id}/scored", response_model=ScoredNotesResponse)
def get_scored_notes_for_promise(
    promise_id: int,
    db: Session = Depends(get_db)
) -> ScoredNotesResponse:
    """
    Get scored notes for a promise with summary statistics.
    
    This is the primary endpoint for displaying community notes on a promise.
    Returns notes ordered by helpfulness (bridging score).
    """
    # Get all notes for this promise
    notes = db.query(Note).filter(Note.promise_id == promise_id).all()
    
    # Calculate summary stats
    total_notes = len(notes)
    helpful_count = sum(1 for n in notes if n.status == NoteStatus.CURRENTLY_RATED_HELPFUL.value)
    needs_ratings_count = sum(1 for n in notes if n.status == NoteStatus.NEEDS_MORE_RATINGS.value)
    
    # Get last scoring time
    last_scored = None
    if notes:
        scored_times = [n.scored_at for n in notes if n.scored_at]
        if scored_times:
            last_scored = max(scored_times)
    
    # Sort: helpful first, then by intercept score
    sorted_notes = sorted(
        notes,
        key=lambda n: (
            0 if n.status == NoteStatus.CURRENTLY_RATED_HELPFUL.value else 1,
            -(n.note_intercept or -999)
        )
    )
    
    return ScoredNotesResponse(
        promise_id=promise_id,
        notes=[_note_to_response(n) for n in sorted_notes],
        total_notes=total_notes,
        helpful_notes_count=helpful_count,
        needs_more_ratings_count=needs_ratings_count,
        last_scored_at=last_scored,
        algorithm_version="1.0.0"
    )


# =============================================================================
# Update Note
# =============================================================================


@router.delete("/{note_id}", status_code=204)
def delete_note(
    note_id: int,
    author_id: int,  # Required to verify ownership
    db: Session = Depends(get_db)
):
    """
    Delete a note.
    
    Only the author can delete their own note. Notes that have been
    rated helpful cannot be deleted (to prevent gaming).
    """
    note = db.query(Note).filter(Note.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    
    if note.author_id != author_id:
        raise HTTPException(status_code=403, detail="Only the author can delete this note")
    
    if note.status == NoteStatus.CURRENTLY_RATED_HELPFUL.value:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a note that has been rated helpful by the community"
        )
    
    db.delete(note)
    db.commit()


# =============================================================================
# Statistics
# =============================================================================


@router.get("/stats/by-classification")
def get_notes_by_classification(
    promise_id: int,
    db: Session = Depends(get_db)
):
    """
    Get count of helpful notes by classification type.
    
    Useful for showing promise status breakdown:
    - How many helpful notes say "promise_kept"
    - How many say "promise_broken"
    - etc.
    """
    results = db.query(
        Note.classification,
        func.count(Note.id).label('count')
    ).filter(
        Note.promise_id == promise_id,
        Note.status == NoteStatus.CURRENTLY_RATED_HELPFUL.value
    ).group_by(Note.classification).all()
    
    return {
        "promise_id": promise_id,
        "classifications": {r.classification: r.count for r in results}
    }


# =============================================================================
# Helper Functions
# =============================================================================


def _note_to_response(note: Note) -> NoteResponse:
    """Convert database Note to response model."""
    return NoteResponse(
        id=note.id,
        promise_id=note.promise_id,
        author_id=note.author_id,
        summary=note.summary,
        content=note.content,
        sources=json.loads(note.sources) if note.sources else [],
        classification=note.classification,
        status=note.status,
        helpfulness_score=note.helpfulness_score,
        note_intercept=note.note_intercept,
        note_factor=note.note_factor,
        helpful_count=note.helpful_count,
        somewhat_helpful_count=note.somewhat_helpful_count,
        not_helpful_count=note.not_helpful_count,
        created_at=note.created_at,
        updated_at=note.updated_at,
        scored_at=note.scored_at
    )
