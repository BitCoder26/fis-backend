from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class EnrolmentApplicationView(APIView):
    def post(self, request):
        return Response({"ok": True}, status=status .HTTP_201_CREATED)

