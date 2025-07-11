from collections.abc import Iterator
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db.models.fields import TextField
from mitoc_const import affiliations

from ws import enums, models, widgets
from ws.membership import MERCHANT_ID, PAYMENT_TYPE
from ws.utils.dates import is_currently_iap, local_now
from ws.utils.signups import non_trip_participants


def _bind_input(
    form: forms.ModelForm | forms.Form,
    field_name: str,
    # Initial value - either from field initial, or instance value (or neither)
    initial: None | str | int | bool = None,
    # (Can optionally use a different model name)
    model_name: str | None = None,
) -> None:
    """Bind the field value in AngularJS.

    This is a janky, home-grown approximation of what Django-Angular does.

    Rather than attempting to bind *every* form field by default (and giving
    the field an automatic `ng-model` setting based on its name), this method
    instead allows selecting binding for just fields referenced in FE code.

    We should aim to delete this entirely, since we want to get off AngularJS.
    """
    field = form.fields[field_name]
    initial = field.initial if initial is None else initial

    if field_name in form.data:
        # (e.g. an invalid POST, we tell Angular about the submitted value)
        initial = form.data[field_name]

    model_name = model_name or field_name
    field.widget.attrs["data-ng-model"] = model_name

    if initial:
        if isinstance(initial, bool):
            js_expr = "true" if initial else "false"
        elif isinstance(initial, str | int):
            js_expr = f"'{initial}'"  # (integers get string values)
        else:
            raise TypeError(f"Unexpected initial value {initial}")

        # Hack to avoid `ng-model` clobbering `value=` (e.g. https://stackoverflow.com/q/10610282)
        field.widget.attrs["data-ng-init"] = f"{model_name} = {js_expr}"


class RequiredModelForm(forms.ModelForm):
    required_css_class = "required"
    error_css_class = "warning"


class ParticipantForm(forms.ModelForm):
    class Meta:
        model = models.Participant
        fields = ["name", "email", "cell_phone", "affiliation"]
        widgets = {
            "name": forms.TextInput(
                attrs={"title": "Full name", "pattern": r"^.* .*$"}
            ),
            "email": forms.Select(),
            "cell_phone": widgets.PhoneInput,
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        user = kwargs.pop("user")

        # Mark any old affiliations as equivalent to blank
        # (Will properly trigger a "complete this field" warning)
        if kwargs.get("instance") and len(kwargs["instance"].affiliation) == 1:
            kwargs["instance"].affiliation = ""

        super().__init__(*args, **kwargs)

        self.verified_emails = user.emailaddress_set.filter(verified=True).values_list(
            "email", flat=True
        )
        self.fields["email"].widget.choices = [
            (email, email) for email in self.verified_emails
        ]

        par = self.instance
        _bind_input(self, "affiliation", initial=par and par.affiliation)

    def clean_affiliation(self):
        """Require a valid MIT email address for MIT student affiliation."""
        mit_student_codes = {
            affiliations.MIT_UNDERGRAD.CODE,
            affiliations.MIT_GRAD_STUDENT.CODE,
        }
        affiliation = self.cleaned_data["affiliation"]
        if affiliation not in mit_student_codes:
            return affiliation  # Nothing extra needs to be done!
        if not any(email.lower().endswith("mit.edu") for email in self.verified_emails):
            raise ValidationError(
                "MIT email address required for student affiliation!",
                code="lacks_mit_email",
            )
        return affiliation


class ParticipantLookupForm(forms.Form):
    """Perform lookup of a given participant, loading on selection."""

    participant = forms.ModelChoiceField(queryset=models.Participant.objects.all())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        participant_field = self.fields["participant"]
        participant_field.help_text = None  # Disable "Hold command..."
        participant_field.label = ""
        initial = kwargs.get("initial")
        if initial and initial.get("participant"):
            participant_field.empty_label = None

        participant_field.widget.attrs["onchange"] = "this.form.submit();"


class CarForm(forms.ModelForm):
    form_name = "car_form"

    def clean_license_plate(self):
        return self.cleaned_data["license_plate"].upper()

    class Meta:
        model = models.Car
        fields = ["license_plate", "state", "make", "model", "year", "color"]
        widgets = {
            "year": forms.NumberInput(
                attrs={"min": model.year_min, "max": model.year_max}
            ),
        }


class EmergencyContactForm(forms.ModelForm):
    class Meta:
        model = models.EmergencyContact
        fields = ["name", "email", "cell_phone", "relationship"]
        widgets = {
            "name": forms.TextInput(
                attrs={"title": "Full name", "pattern": r"^.* .*$"}
            ),
            "email": forms.TextInput(),
            "cell_phone": widgets.PhoneInput,
        }


class EmergencyInfoForm(forms.ModelForm):
    class Meta:
        model = models.EmergencyInfo
        fields = ["allergies", "medications", "medical_history"]
        widgets = {"medical_history": forms.Textarea(attrs={"rows": 5})}


class LeaderRecommendationForm(forms.ModelForm):
    class Meta:
        model = models.LeaderRecommendation
        exclude: list[str] = []


class ApplicationLeaderForm(forms.ModelForm):
    """Form for assigning a rating from a leader application.

    Since the participant and activity are given by the application itself,
    we need not include those an options in the form.
    """

    is_recommendation = forms.BooleanField(required=False, label="Is a recommendation")

    class Meta:
        model = models.LeaderRating
        fields = ["rating", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 1})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        _bind_input(
            self,
            "is_recommendation",
            model_name="is_rec",
            initial=self.initial.get("is_recommendation", False),
        )


class LeaderForm(forms.ModelForm):
    """Allows assigning a rating to participants in any allowed activity."""

    def __init__(self, *args, **kwargs):
        allowed_activities = kwargs.pop("allowed_activities", None)
        hide_activity = kwargs.pop("hide_activity", False)

        super().__init__(*args, **kwargs)

        all_par = models.Participant.objects.all()
        self.fields["participant"].queryset = all_par
        self.fields["participant"].empty_label = "Nobody"

        if allowed_activities is not None:
            allowed_activity_values = {
                activity_enum.value for activity_enum in allowed_activities
            }
            activities = [
                (val, label)
                for (val, label) in self.fields["activity"].choices
                if val in allowed_activity_values
            ]
            self.fields["activity"].choices = activities
            if activities:
                self.fields["activity"].initial = activities[0]
        if hide_activity:  # Note: We currently *always* hide the activity
            self.fields["activity"].widget = forms.HiddenInput()

        # Give each field an ng-model so that the `leaderRating` controller can manage the form.
        # (We query ratings for a given participant + activity, then set rating & notes with the result)

        # (No `ng-init` since this is a plain widget & starts unselected)
        self.fields["participant"].widget.attrs["data-ng-model"] = "participant"

        # Activity is *always* set, since it's a hidden field
        _bind_input(self, "activity", initial=self.initial["activity"])
        # Technically, neither of these need the `ng-init` hack, since they start blank
        _bind_input(self, "rating")
        _bind_input(self, "notes")

    class Meta:
        model = models.LeaderRating
        fields = ["participant", "activity", "rating", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "participant": widgets.ParticipantSelect,
        }


class TripSearchForm(forms.Form):
    q = forms.CharField(required=False)
    trip_type = forms.ChoiceField(
        required=False,
        label=models.Trip.trip_type.field.verbose_name,
        choices=[("", "Any"), *enums.TripType.choices()],
    )
    program = forms.ChoiceField(
        required=False,
        choices=[("", "Any"), *enums.Program.choices()],
    )

    assert models.Trip.winter_terrain_level.field.choices is not None
    winter_terrain_level = forms.ChoiceField(
        required=False,
        label="Terrain level",
        choices=[
            ("", "Any"),
            *[
                (level, level)
                for (
                    level,
                    _label,
                ) in models.Trip.winter_terrain_level.field.choices
            ],
        ],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs["placeholder"] = "Franconia Notch..."
        self.fields["q"].label = False


class TripInfoForm(forms.ModelForm):
    accurate = forms.BooleanField(
        required=True,
        label="I affirm that all participant and driver information is correct",
    )

    class Meta:
        model = models.TripInfo
        fields = [
            "drivers",
            "start_location",
            "start_time",
            "turnaround_time",
            "return_time",
            "worry_time",
            "itinerary",
        ]


class TripForm(forms.ModelForm):
    class Meta:
        model = models.Trip
        fields = [
            "edit_revision",
            # Basics
            "name",
            "program",
            "trip_type",
            "maximum_participants",
            "difficulty_rating",
            "winter_terrain_level",
            "leaders",
            "wimp",
            # Settings
            "membership_required",
            "allow_leader_signups",
            "honor_participant_pairing",
            "let_participants_drop",
            # About
            "description",
            "summary",
            "prereqs",
            # Signup
            "trip_date",
            "algorithm",
            "signups_open_at",
            "signups_close_at",
            "notes",
        ]
        ex_notes = (
            " 1. Do you have any dietary restrictions?\n"
            " 2. What's your experience level?\n"
            " 3. What are you most excited about?\n"
        )
        ex_descr = "\n".join(
            [
                "We'll be heading up into the [Whites][whites] "
                "for a ~~day~~ weekend of exploring!",
                "",
                "### Why?",
                "Because it's _fun_!",
                "",
                "Prerequisites:",
                " - Enthusiastic attitude",
                " - Prior experience",
                " - **Proper clothing**",
                "",
                "[whites]: https://wikipedia.org/wiki/White_Mountains_(New_Hampshire)",
            ]
        )

        widgets = {
            "leaders": widgets.LeaderSelect,
            "wimp": widgets.ParticipantSelect,
            "description": widgets.MarkdownTextarea(ex_descr),
            "notes": widgets.MarkdownTextarea(ex_notes),
            "trip_date": forms.DateInput(attrs={"type": "date"}),
            "signups_open_at": widgets.DateTimeLocalInput,
            "signups_close_at": widgets.DateTimeLocalInput,
            "edit_revision": forms.HiddenInput(),
        }

    def clean_membership_required(self):
        """Ensure that all WS trips require current dues."""
        if self.cleaned_data.get("program") == enums.Program.WINTER_SCHOOL.value:
            return True
        return self.cleaned_data["membership_required"]

    def clean_maximum_participants(self):
        trip = self.instance
        new_max = self.cleaned_data["maximum_participants"]
        if trip.pk is None:  # Trip not yet created, any max participants value is fine.
            return new_max
        accepted_signups = trip.signup_set.filter(on_trip=True).count()
        if self.instance and accepted_signups > new_max:
            raise ValidationError(
                "Can't shrink trip past number of signed-up participants. "
                "To remove participants, admin this trip instead."
            )
        return new_max

    def clean(self):
        """Ensure that all leaders can lead the trip."""
        super().clean()

        if "program" not in self.cleaned_data or "leaders" not in self.cleaned_data:
            return self.cleaned_data
        leaders = self.cleaned_data["leaders"]
        program_enum = enums.Program(self.cleaned_data["program"])

        # To allow editing old trips with lapsed leaders, only check new additions
        trip = self.instance
        if trip.pk:
            leaders = leaders.exclude(pk__in=trip.leaders.all())

        lacking_privs = [par for par in leaders if not par.can_lead(program_enum)]

        if lacking_privs:
            names = ", ".join(leader.name for leader in lacking_privs)
            self.add_error("leaders", f"{names} can't lead {program_enum.label} trips")
        return self.cleaned_data

    def clean_winter_terrain_level(self) -> str | None:
        """Strip terrain level if not WS."""
        program = self.cleaned_data.get("program")
        if program and not enums.Program(program).winter_rules_apply():
            return None
        level = self.cleaned_data.get("winter_terrain_level", "")
        assert isinstance(level, str)
        return level

    def _init_wimp(self):
        """Configure the WIMP widget, load saved participant if applicable."""
        wimp = self.fields["wimp"].widget
        wimp.attrs["msg"] = "'Nobody'"
        wimp.attrs["exclude_self"] = "true"

        if self.instance.wimp:
            wimp.attrs["selected-id"] = self.instance.wimp.pk
            wimp.attrs["selected-name"] = self.instance.wimp.name

    def _allowed_program_choices(self, allowed_program_enums):
        # If editing an existing trip, the old program can persist.
        if self.instance and self.instance.program_enum not in allowed_program_enums:
            allowed_program_enums = [self.instance.program_enum, *allowed_program_enums]

        for category, choices in enums.Program.choices():
            assert isinstance(category, str)
            assert isinstance(choices, list)
            valid_choices = [
                (value, label)
                for (value, label) in choices
                if enums.Program(value) in allowed_program_enums
            ]
            if valid_choices:
                yield (category, valid_choices)

    def __init__(self, *args, **kwargs):
        allowed_programs = kwargs.pop("allowed_programs", None)
        super().__init__(*args, **kwargs)
        trip = self.instance

        self.fields["summary"].required = False

        if trip.pk is None:  # New trips must have *all* dates/times in the future
            now = local_now()
            # (the `datetime-local` inputs don't take timezones at all)
            # We specify only to the minutes' place so that we don't display seconds
            naive_iso_now = now.replace(tzinfo=None).isoformat(timespec="minutes")

            self.fields["trip_date"].widget.attrs["min"] = now.date().isoformat()
            # There is *extra* dynamic logic that (open < close at <= trip date)
            # However, we can at minimum enforce that times occur in the future
            self.fields["signups_open_at"].widget.attrs["min"] = naive_iso_now
            self.fields["signups_close_at"].widget.attrs["min"] = naive_iso_now

        # Use the participant queryset to cover an edge case:
        # editing an old trip where one of the leaders is no longer a leader!
        self.fields["leaders"].queryset = models.Participant.objects.get_queryset()
        self.fields["leaders"].help_text = None  # Disable "Hold command..."

        # We'll dynamically hide the level widget on GET if it's not a winter trip.
        # If it *is* displayed though, it should be rendered with `required`.
        # On POST, we only want this field required for winter trips.
        if self.data.get("program"):  # Loading trip or creating new one
            program = self.data["program"]
            program_enum = enums.Program(program) if program else None
            self.fields["winter_terrain_level"].required = (
                program_enum and program_enum.winter_rules_apply()
            )
        else:
            # New trip -- say that field is required.
            self.fields["winter_terrain_level"].required = True

        initial_program = trip.pk and trip.program_enum
        if allowed_programs is not None:
            self.fields["program"].choices = list(
                self._allowed_program_choices(allowed_programs)
            )

            # If it's currently WS, the WS program is almost certainly what's desired for new trips.
            if (
                enums.Program.WINTER_SCHOOL in allowed_programs
                and is_currently_iap()
                and not trip.pk
            ):
                initial_program = enums.Program.WINTER_SCHOOL

        self._init_wimp()

        # (No need for `ng-init`, we have a custom directive)
        self.fields["leaders"].widget.attrs["data-ng-model"] = "leaders"

        _bind_input(self, "program", initial=initial_program and initial_program.value)
        _bind_input(self, "algorithm", initial=trip and trip.algorithm)


class SignUpForm(forms.ModelForm):
    class Meta:
        model = models.SignUp
        fields = ["trip", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}

    def clean_notes(self):
        signup_notes = self.cleaned_data["notes"].strip()
        if "trip" not in self.cleaned_data:
            return signup_notes

        trip = self.cleaned_data["trip"]
        if trip.notes and not signup_notes:
            raise ValidationError("Please complete notes to sign up!")
        return signup_notes

    def __init__(self, *args, **kwargs):
        """Set notes to required if trip notes are present.

        Trips should always be given via initial. We can check if the trip
        has a notes field this way.
        """
        super().__init__(*args, **kwargs)
        trip = self.initial.get("trip")
        if trip and trip.notes:
            notes = self.fields["notes"]
            notes.required = True
            notes.widget.attrs["placeholder"] = trip.notes
            notes.widget.attrs["rows"] = max(4, trip.notes.count("\n") + 1)


class LeaderSignUpForm(SignUpForm):
    class Meta:
        model = models.LeaderSignUp
        fields = ["trip", "notes"]


class LeaderParticipantSignUpForm(RequiredModelForm):
    """For leaders to sign up participants. Notes aren't required."""

    top_spot = forms.BooleanField(
        required=False,
        label="Move to top spot",
        help_text=(
            "Move the participant above other prioritized waitlist "
            "spots (e.g. participants previously added with this form, "
            "or those who were bumped off to allow a driver on)"
        ),
    )

    class Meta:
        model = models.SignUp
        fields = ["participant", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, trip, *args, **kwargs):
        super().__init__(*args, **kwargs)
        non_trip = non_trip_participants(trip)
        self.fields["participant"].queryset = non_trip
        self.fields["participant"].help_text = None  # Disable "Hold command..."


class LotteryInfoForm(forms.ModelForm):
    class Meta:
        model = models.LotteryInfo
        fields = ["car_status", "number_of_passengers"]
        widgets = {
            "number_of_passengers": forms.NumberInput(
                attrs={"min": 0, "max": 13}  # hard-coded, but matches model
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for attr in ("car_status", "number_of_passengers"):
            _bind_input(self, attr, initial=getattr(self.instance, attr, None))


class LotteryPairForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        participant = kwargs.pop("participant")

        super().__init__(*args, **kwargs)

        paired_with = self.fields["paired_with"]
        paired_with.queryset = models.Participant.objects.exclude(pk=participant.pk)
        paired_with.empty_label = "Nobody"

        # Set up arguments to be passed to Angular directive
        widget = paired_with.widget
        widget.attrs["data-ng-model"] = "paired_with"
        widget.attrs["data-msg"] = "'Nobody'"
        widget.attrs["data-exclude_self"] = "true"

        if self.instance.paired_with:
            widget.attrs["data-selected-id"] = self.instance.paired_with.pk
            widget.attrs["data-selected-name"] = self.instance.paired_with.name

    class Meta:
        model = models.LotteryInfo
        fields = ["paired_with"]
        widgets = {"paired_with": widgets.ParticipantSelect}


class FeedbackForm(RequiredModelForm):
    class Meta:
        model = models.Feedback
        fields = ["comments", "showed_up"]


class AttendedLecturesForm(forms.Form):
    participant = forms.ModelChoiceField(queryset=models.Participant.objects.all())


class WinterSchoolSettingsForm(forms.ModelForm):
    class Meta:
        model = models.WinterSchoolSettings
        fields = ["allow_setting_attendance", "accept_applications"]


# TODO: This should be a class, not a method.
def LeaderApplicationForm(*args: Any, **kwargs: Any) -> forms.ModelForm:  # noqa: N802
    """Factory form for applying to be a leader in any activity."""
    activity_enum: enums.Activity = kwargs.pop("activity_enum")

    class DynamicActivityForm(forms.ModelForm):
        class Meta:
            exclude = (  # noqa: DJ006
                "archived",
                "year",
                "participant",
                "previous_rating",
            )
            model = models.LeaderApplication.model_from_activity(activity_enum)
            widgets = {
                field.name: forms.Textarea(attrs={"rows": 4})
                for field in model._meta.fields
                if isinstance(field, TextField)
            }

        def clean(self):
            cleaned_data = super().clean()
            if not models.LeaderApplication.accepting_applications(activity_enum):
                raise ValidationError("Not currently accepting applications!")
            return cleaned_data

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # TODO: Errors on args, where args is a single tuple of the view
            # super().__init__(*args, **kwargs)
            super().__init__(**kwargs)

            # For fields which are conditionally shown/hidden, set the required attr
            # Critically, we must *not* actually make the *field* required.
            # The idea is to just tell the browser that the input is required.
            # (we don't want to fail a form submission for somebody who doesn't want mentorship)
            #
            # We should *perhaps* reconsider this hack to make the application work without JS
            # (we use JavaScript to conditionally hide this div)
            for conditional_field in ("mentee_activities", "mentor_activities"):
                if conditional_field in self.fields:
                    self.fields[conditional_field].widget.attrs["required"] = True

    return DynamicActivityForm(*args, **kwargs)


AffiliationChoice = tuple[str | int, str]


def amount_choices(
    value_is_amount: bool = False,
) -> Iterator[
    tuple[str, list[AffiliationChoice]]  # Grouped list of affiliations
    | AffiliationChoice  # Single choice
]:
    """Yield all affiliation choices with the price in the label.

    If `value_is_amount` is True, we'll replace the two-letter affiliation
    with the price as the choice's value.
    """

    def include_amount_in_label(
        affiliation_code: str, label: str
    ) -> tuple[int | str, str]:
        amount = models.Participant.affiliation_to_annual_dues(affiliation_code)
        annotated_label = f"{label} (${amount})"

        if value_is_amount:
            return (amount, annotated_label)
        return (affiliation_code, annotated_label)

    for label, option in models.Participant.AFFILIATION_CHOICES:
        if isinstance(option, list):
            # The options are a collection of affiliation codes
            yield label, [include_amount_in_label(*choice) for choice in option]
        else:
            # It's a top-level choice - the label & option are actually switched
            yield include_amount_in_label(label, option)


class DuesForm(forms.Form):
    """Provide a form that's meant to submit its data to CyberSource.

    Specifically, each of these named fields is what's expected for MIT's
    payment system to process a credit card payment and link it to user-supplied
    metadata. For example, `merchantDefinedData3` is the MITOCer's email address.

    The expected URL is https://shopmitprd.mit.edu/controller/index.php
    """

    merchant_id = forms.CharField(widget=forms.HiddenInput(), initial=MERCHANT_ID)
    description = forms.CharField(
        widget=forms.HiddenInput(),
        # Keep this description even though we may not consider anybody paying dues a "member."
        initial="membership fees.",
    )

    merchantDefinedData1 = forms.CharField(  # noqa: N815
        widget=forms.HiddenInput(), initial=PAYMENT_TYPE
    )
    merchantDefinedData2 = forms.ChoiceField(  # noqa: N815
        required=True, label="Affiliation", choices=list(amount_choices())
    )
    merchantDefinedData3 = forms.EmailField(required=True, label="Email")  # noqa: N815
    merchantDefinedData4 = forms.CharField(  # noqa: N815
        required=True,
        label="Full name",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Tim Beaver",
                "title": "Full name",
                "pattern": r"^.* .*$",
            },
        ),
    )

    # For Participant-less users with JS enabled, this will be hidden & silently
    # set by an Angular directive that updates the amount based on the affiliation.
    # For users _without_ JavaScript, it will display as a Select widget.
    amount = forms.ChoiceField(
        label="Please confirm your affiliation",
        required=True,
        help_text="(We're showing this because you have scripts disabled)",
        choices=list(amount_choices(value_is_amount=True)),
    )

    def __init__(self, *args, **kwargs):
        participant = kwargs.pop("participant")

        super().__init__(*args, **kwargs)
        email = self.fields["merchantDefinedData3"]

        if not participant:
            email.widget.attrs["placeholder"] = "tim@mit.edu"
            # Without this, the default choice is 'Undergraduate student'.
            # This heading doesn't render as a choice, but it behaves like one.
            self.fields["amount"].initial = ""
        if participant:
            email.initial = participant.email
            self.fields["merchantDefinedData4"].initial = participant.name
            self.fields["merchantDefinedData2"].initial = participant.affiliation
            self.fields["amount"].initial = participant.annual_dues

        # Affiliation is bound so that we can:
        # - warn anybody selecting an MIT affiliation that a @*mit.edu email is required
        # - set the amount to pay whenever affiliation changes
        _bind_input(self, "merchantDefinedData2", model_name="affiliation")
        # Email is bound so we can require an MIT email address for MIT rates
        _bind_input(self, "merchantDefinedData3", model_name="email")
        # Amount is bound so that we can:
        # - set a value based on which affiliation is chosen
        # - show the dollar amount to end users in the submit button
        _bind_input(self, "amount")


class WaiverForm(forms.Form):
    name = forms.CharField(required=True)
    email = forms.EmailField(required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update(
            {
                "title": "Full legal name",
                "pattern": r"^.* .*$",
                "placeholder": "Tim Beaver",
            }
        )
        self.fields["email"].widget.attrs["placeholder"] = "tim@mit.edu"


class GuardianForm(forms.Form):
    name = forms.CharField(required=True, label="Parent or Guardian Name")
    email = forms.EmailField(required=True, label="Parent or Guardian Email")


class PrivacySettingsForm(forms.ModelForm):
    class Meta:
        model = models.Participant
        fields = ["gravatar_opt_out"]


class EmailPreferencesForm(forms.ModelForm):
    class Meta:
        model = models.Participant
        fields = ["send_membership_reminder"]
